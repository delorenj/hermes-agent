"""Monitor-mode cron support — hash-suppressed change detection.

The source is evaluated on each scheduled tick. A changed observation stays pending until an
agent run completes; only then does its exact output become the acknowledged hash and diff
snapshot. Refused or failed runs leave the acknowledged baseline untouched.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Prompt-injection caps: unified diff, and new-output block (mirrors the 8k context_from truncation
# in cron/scheduler.py). Then bounded-GET limits for monitor_url sources.
MAX_DIFF_CHARS = 4000
MAX_OUTPUT_CHARS = 8000
URL_TIMEOUT_SECONDS = 30
MAX_URL_BYTES = 262_144  # 256 KiB

_SNAPSHOT_FILENAME = "monitor_last_output.txt"


@dataclass
class MonitorOutcome:
    """Result of one monitor-source evaluation."""

    ok: bool
    changed: bool = False
    first_run: bool = False
    context_block: Optional[str] = None
    error: Optional[str] = None
    output: str = ""
    output_hash: str = ""
    previous_hash: Optional[str] = None


def hash_monitor_output(output: str) -> str:
    """Hash the monitor output as exact UTF-8 bytes (no normalization)."""
    return hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()


def build_monitor_diff(old: str, new: str) -> str:
    """Unified diff of old vs new monitor output, capped at MAX_DIFF_CHARS."""
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="previous", tofile="current", lineterm="",
        )
    )
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return diff


def _snapshot_path(job_id: str):
    from cron.jobs import _job_output_dir

    return _job_output_dir(job_id) / _SNAPSHOT_FILENAME


def _read_last_output(job_id: str) -> str:
    try:
        path = _snapshot_path(job_id)
        if path.exists():
            return path.read_text(encoding="utf-8-sig")
    except Exception as exc:
        logger.warning("Monitor: failed to read last output for %r: %s", job_id, exc)
    return ""


def _fetch_monitor_url(url: str) -> tuple[bool, str]:
    """Bounded GET of a monitor URL. Returns (ok, body-or-error)."""
    import urllib.request

    if not str(url).lower().startswith(("http://", "https://")):
        return False, f"monitor_url must be http(s): {url!r}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-cron-monitor"})
        with urllib.request.urlopen(req, timeout=URL_TIMEOUT_SECONDS) as resp:  # nosec B310 — scheme checked above
            body = resp.read(MAX_URL_BYTES + 1)
        return True, body[:MAX_URL_BYTES].decode("utf-8", errors="replace")
    except Exception as exc:
        return False, f"monitor_url fetch failed: {exc}"


def _field(job: dict, key: str) -> str:
    return (job.get(key) or "").strip()


def _run_monitor_source(job: dict) -> tuple[bool, str]:
    """Run the job's monitor source (script or URL). Returns (ok, output)."""
    monitor_script = _field(job, "monitor_script")
    if monitor_script:
        # Same containment + interpreter rules as the existing `script` field.
        from cron.scheduler_script import _run_job_script

        return _run_job_script(monitor_script, workdir=_field(job, "workdir") or None,
                               interpreter=job.get("interpreter"))
    monitor_url = _field(job, "monitor_url")
    if monitor_url:
        return _fetch_monitor_url(monitor_url)
    return False, "monitor job has neither monitor_script nor monitor_url"


def job_has_monitor(job: dict) -> bool:
    return bool(_field(job, "monitor_script") or _field(job, "monitor_url"))


def check_monitor(job: dict) -> MonitorOutcome:
    """Observe the source; record pending work without advancing the acknowledged baseline."""
    job_id = str(job.get("id") or "")
    ok, output = _run_monitor_source(job)
    if not ok:
        return MonitorOutcome(ok=False, error=output)

    new_hash = hash_monitor_output(output)
    from cron.jobs import _with_job, save_jobs

    def observe(jobs, i, stored):
        raw_state = stored.get("monitor_state")
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        last_hash = state.get("last_output_hash")
        if last_hash == new_hash:
            if state.pop("pending_output_hash", None) is not None:
                stored["monitor_state"] = state
                save_jobs(jobs)
            return last_hash
        state["pending_output_hash"] = new_hash
        stored["monitor_state"] = state
        save_jobs(jobs)
        return last_hash

    last_hash = _with_job(job_id, observe)
    if last_hash is not None and new_hash == last_hash:
        return MonitorOutcome(ok=True, changed=False)

    first_run = last_hash is None
    old_output = "" if first_run else _read_last_output(job_id)
    shown_output = output
    if len(shown_output) > MAX_OUTPUT_CHARS:
        shown_output = shown_output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"

    current = f"### Current output\n\n```\n{shown_output}\n```"
    if first_run:
        context_block = (
            "## Monitor Baseline (first run)\n\n"
            "This is the first observation of the monitored source — there is "
            "no previous output to diff against.\n\n" + current
        )
    else:
        diff = build_monitor_diff(old_output, output)
        context_block = (
            "## MONITOR CHANGE DETECTED\n\n"
            "The monitored source's output changed since the last run.\n\n"
            f"### Diff (previous → current)\n\n```diff\n{diff}\n```\n\n" + current
        )

    return MonitorOutcome(ok=True, changed=True, first_run=first_run, context_block=context_block,
                          output=output, output_hash=new_hash, previous_hash=last_hash)


def acknowledge_monitor(job_id: str, observation: MonitorOutcome) -> bool:
    """Advance the baseline only for the exact pending observation a completed agent saw."""
    if not observation.changed or not observation.output_hash:
        return False
    from cron.jobs import _hermes_now, _with_job, save_jobs

    def acknowledge(jobs, i, stored):
        raw_state = stored.get("monitor_state")
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        if (state.get("pending_output_hash") != observation.output_hash
                or state.get("last_output_hash") != observation.previous_hash):
            return False
        path = _snapshot_path(job_id)
        from cron.jobs import _ensure_cron_dir
        _ensure_cron_dir(path.parent)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(observation.output, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        state.pop("pending_output_hash", None)
        state.update(last_output_hash=observation.output_hash,
                     last_changed_at=_hermes_now().isoformat())
        stored["monitor_state"] = state
        save_jobs(jobs)
        return True

    return bool(_with_job(job_id, acknowledge))
