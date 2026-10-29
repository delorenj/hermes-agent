"""Monitor-mode cron support — hash-suppressed change detection.

The source is evaluated on each scheduled tick. A changed observation stays pending until
owner-fenced terminal semantic execution success (not notification delivery). Bare run_job
completion is provisional; failures, empty responses and interrupted/stale runs cannot ack.

This is latest-observation-wins snapshot state, NOT an event queue. A newer observation of
acknowledged A supersedes in-flight B; B must not overwrite A. Use an event queue if every
transient transition needs processing. Pending work retries on the existing bounded schedule.

The atomic jobs store's last_output_hash selects an immutable snapshot. The fixed legacy
monitor_last_output.txt is only a projection, repaired BEFORE a source can read it (including
ZCRANK_GATE_LAST), so a crash after the hash commit cannot feed a stale baseline to a gate.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
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


def _immutable_snapshot_path(job_id: str, output_hash: str):
    if not re.fullmatch(r"[0-9a-f]{64}", output_hash):
        raise ValueError("Invalid acknowledged monitor hash")
    return _snapshot_path(job_id).parent / "monitor_snapshots" / f"{output_hash}.txt"


def _fsync_snapshot_directory(path) -> None:
    # A new hash must not commit if its object directory could disappear on power loss.
    # Windows has no directory-fsync API; file fsync + atomic publication still apply.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_snapshot_write(path, output: str, *, immutable: bool = False) -> None:
    from cron.jobs import _ensure_cron_dir

    _ensure_cron_dir(path.parent)
    # The job output directory may itself have just been created by save_job_output.
    _fsync_snapshot_directory(path.parent.parent.parent)
    _fsync_snapshot_directory(path.parent.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(output.encode("utf-8", errors="replace"))
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(temporary, path)  # publish once; never replace an acknowledged object
            except FileExistsError:
                if path.read_bytes() != temporary.read_bytes():
                    raise OSError("Monitor snapshot hash collision or corrupt object")
        else:
            os.replace(temporary, path)
        _fsync_snapshot_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_last_output(job_id: str, output_hash: str) -> str:
    output = _immutable_snapshot_path(job_id, output_hash).read_bytes().decode("utf-8")
    if hash_monitor_output(output) != output_hash:
        raise OSError("Acknowledged monitor snapshot is corrupt")
    return output


def _repair_snapshot(job_id: str, output_hash: Optional[str]) -> None:
    """Caller holds the jobs lock: recover the compatibility projection, or migrate legacy data."""
    alias = _snapshot_path(job_id)
    if output_hash is None:
        # An unacknowledged first-run snapshot must never become a script's baseline.
        alias.unlink(missing_ok=True)
        return
    immutable = _immutable_snapshot_path(job_id, output_hash)
    if not immutable.exists():
        legacy = alias.read_bytes().decode("utf-8")
        if hash_monitor_output(legacy) != output_hash:
            raise OSError("Legacy monitor snapshot does not match acknowledged hash")
        _atomic_snapshot_write(immutable, legacy, immutable=True)
    output = _read_last_output(job_id, output_hash)
    if not alias.exists() or alias.read_bytes() != output.encode("utf-8"):
        _atomic_snapshot_write(alias, output)


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
    from cron.jobs import _with_job, save_jobs

    try:
        _with_job(job_id, lambda _jobs, _i, stored: _repair_snapshot(
            job_id, (stored.get("monitor_state") or {}).get("last_output_hash")))
    except Exception as exc:
        return MonitorOutcome(ok=False, error=f"Monitor snapshot recovery failed: {exc}")
    ok, output = _run_monitor_source(job)
    if not ok:
        return MonitorOutcome(ok=False, error=output)

    new_hash = hash_monitor_output(output)

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
    old_output = "" if first_run else _read_last_output(job_id, last_hash)
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
    """Commit the exact latest observation; caller must hold the terminal-success owner fence."""
    if not observation.changed or not observation.output_hash:
        return False
    from cron.jobs import _hermes_now, _with_job, save_jobs

    def acknowledge(jobs, i, stored):
        raw_state = stored.get("monitor_state")
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        if (state.get("pending_output_hash") != observation.output_hash
                or state.get("last_output_hash") != observation.previous_hash):
            return False
        if hash_monitor_output(observation.output) != observation.output_hash:
            raise ValueError("Monitor observation hash does not match its output")
        _atomic_snapshot_write(_immutable_snapshot_path(job_id, observation.output_hash),
                               observation.output, immutable=True)
        state.pop("pending_output_hash", None)
        state.update(last_output_hash=observation.output_hash,
                     last_changed_at=_hermes_now().isoformat())
        stored["monitor_state"] = state
        save_jobs(jobs)  # atomic hash/pointer commit; an unreferenced object is harmless on failure
        _repair_snapshot(job_id, observation.output_hash)
        return True

    return bool(_with_job(job_id, acknowledge))


def finish_monitor_run(delivery, fire_owner: Optional[str], execution_id: str) -> bool:
    """Hold the fire lock across claim retirement, ledger CAS and monitor hash commit.

    mark_job_run retires the claim. Releasing its lock before ack would let a new owner
    replace us in that gap. Non-monitor jobs keep their existing completion path.
    """
    from cron import scheduler
    from cron.jobs import _fire_job_lock

    if delivery.job.get("_monitor_observation") is None:
        return scheduler._finish_completed_run(delivery, fire_owner, execution_id)
    with _fire_job_lock(delivery.job["id"]) as acquired:
        if not acquired:
            scheduler._record_fire_ownership_lost(delivery.job, fire_owner, execution_id)
            return True
        return scheduler._finish_completed_run(delivery, fire_owner, execution_id)


def acknowledge_completed_monitor(job: dict, receipt: Optional[dict], *, success: bool) -> None:
    """Only a new durable terminal success receipt can acknowledge a provisional observation."""
    observation = job.pop("_monitor_observation", None)
    if not success or not receipt or receipt.get("status") != "completed" or observation is None:
        return
    try:
        acknowledge_monitor(job["id"], observation)
    except Exception:
        # A pre-commit failure leaves pending intact. A post-commit alias failure recovers
        # from the acknowledged hash before the next source evaluation; never roll it back.
        logger.exception("Job '%s': monitor snapshot commit/projection failed; recovery on next tick", job["id"])
