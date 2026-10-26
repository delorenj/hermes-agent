"""``hermes cron run <job> --wait``: trigger a job and block until its durable attempt is terminal.

An external scheduler (Candybar, AD-19) owns WHEN a ``scheduler: external`` job fires; this is the
trigger half of that contract. The run itself is whatever ``hermes cron run`` already does (inline,
background, or handed to the gateway ticker). ``--wait`` then follows the attempt in the executions
ledger to a terminal state, so the caller gets one process whose lifetime IS the attempt:

* exit 0 when the attempt is ``completed``, exit 1 for anything else (``failed``, ``unknown``, a
  trigger refusal, no attempt observed, a timeout, or an owner that died);
* the last stdout line is ``cron-run-result: <json>`` carrying the attempt id, status, error and the
  token usage read back from ``cron/usage_audit.jsonl`` (0 when the run never reached the model).

Double-trigger: the claim CAS in ``claim_job_for_fire`` already refuses a second concurrent attempt.
A second ``--wait`` caller therefore JOINS the attempt already in flight and reports its outcome —
never two attempts for one job.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

RESULT_PREFIX = "cron-run-result: "

_TERMINAL = frozenset({"completed", "failed", "unknown"})
_POLL_SECONDS = 1.0
# How long a trigger may go without ANY attempt row appearing (ticker hand-off waits for the next
# tick; everything else creates the row inside the trigger itself).
_NO_ATTEMPT_GRACE_SECONDS = 150.0
_AUDIT_TAIL_BYTES = 4 * 1024 * 1024
# The attempt row's claimed_at is stamped by the same clock moments after ``started``; allow skew
# so a row created in the same instant as the trigger is never mistaken for an older attempt.
_CLAIM_SLACK = timedelta(seconds=2)


def _aware(value: Any) -> Optional[datetime]:
    from cron.jobs import _parse_aware

    return _parse_aware(value) if value else None


def read_usage(execution_id: str) -> dict[str, int]:
    """Token usage the run recorded for this attempt (zeros when it never reached the model)."""
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    try:
        from cron.scheduler import _usage_audit_path

        path = _usage_audit_path()
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _AUDIT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return usage
    for line in reversed(tail.splitlines()):
        if execution_id not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("execution_id") != execution_id:
            continue

        def _count(key: str) -> int:
            try:
                return max(0, int(record.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        usage["input_tokens"] = _count("prompt_tokens")
        usage["output_tokens"] = _count("completion_tokens")
        usage["total_tokens"] = _count("total_tokens") or (
            usage["input_tokens"] + usage["output_tokens"])
        return usage
    return usage


def _find_attempt(job_id: str, started: datetime) -> Optional[dict[str, Any]]:
    """The attempt this trigger produced or joined: the newest row claimed at/after the trigger, else
    the newest row still in flight (a run already underway when we triggered — we joined it)."""
    from cron.executions import list_executions

    for record in list_executions(job_id=job_id, limit=5):
        claimed = _aware(record.get("claimed_at"))
        if claimed is not None and claimed >= started - _CLAIM_SLACK:
            return record
        if record.get("status") not in _TERMINAL:
            return record
    return None


def _owner_dead(record: dict[str, Any]) -> bool:
    from cron.executions import _owner_is_live

    try:
        return not _owner_is_live(int(record["pid"]), record.get("process_started_at"))
    except (KeyError, TypeError, ValueError):
        return False


def _emit(job_id: str, status: str, error: Optional[str], execution_id: Optional[str]) -> None:
    usage = read_usage(execution_id) if execution_id else {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    payload = {"job_id": job_id, "execution_id": execution_id, "status": status, "error": error, **usage}
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def run_and_wait(
    job_ref: str,
    *,
    timeout: float,
    trigger: Callable[[str], int],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Trigger ``job_ref`` through ``trigger`` (the plain ``hermes cron run`` action) and wait.

    ``timeout`` seconds bounds the whole wait; 0 or less means no bound (the caller owns it)."""
    from cron.executions import get_execution
    from cron.jobs import resolve_job_ref
    from hermes_time import now as hermes_now

    job = resolve_job_ref(job_ref)
    if not job:
        print(f"Failed to run job: no job matches {job_ref!r}", file=sys.stderr)
        _emit(str(job_ref), "failed", f"no job matches {job_ref!r}", None)
        return 1
    job_id = str(job["id"])
    started = hermes_now()
    deadline = monotonic() + timeout if timeout and timeout > 0 else None

    rc = trigger(job_ref)
    if rc != 0:
        _emit(job_id, "failed", "trigger refused", None)
        return 1

    waited_for_row = monotonic()
    attempt: Optional[dict[str, Any]] = None
    while True:
        if attempt is None:
            attempt = _find_attempt(job_id, started)
        else:
            attempt = get_execution(str(attempt["id"])) or attempt
        if attempt is not None and attempt.get("status") in _TERMINAL:
            break
        if attempt is not None and _owner_dead(attempt):
            _emit(job_id, "failed",
                  "attempt owner exited before a terminal state (whether side effects ran is unknown)",
                  str(attempt["id"]))
            return 1
        now = monotonic()
        if attempt is None and now - waited_for_row > _NO_ATTEMPT_GRACE_SECONDS:
            _emit(job_id, "failed", "no execution attempt was observed after the trigger", None)
            return 1
        if deadline is not None and now >= deadline:
            _emit(job_id, "failed", f"timed out after {timeout:g}s waiting for the attempt",
                  str(attempt["id"]) if attempt else None)
            return 1
        sleep(_POLL_SECONDS)

    status = str(attempt.get("status"))
    error = attempt.get("error") if status != "completed" else None
    _emit(job_id, status, error, str(attempt["id"]))
    return 0 if status == "completed" else 1
