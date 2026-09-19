"""Admission control for unattended cron inference.

The scheduler's provider spend limit is necessary but not sufficient: a job
can remain under a daily dollar ceiling while producing no useful change for
hundreds of identical fires.  This module keeps the admission decision cheap,
deterministic, and independent of the provider.  OpenRouter (or another
provider) remains the source of truth for final billing reconciliation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional


DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "suppress_identical_prompts": True,
    "dedupe_window_seconds": 86400,
    "max_prompt_tokens": 128000,
    "max_estimated_tokens": 256000,
    "max_iterations": 12,
    "max_daily_runs": 24,
    "max_daily_tokens": 1000000,
    "max_run_usd": "1.00",
    "max_daily_usd": "5.00",
    "alert_after_noop": 3,
}

_UTC = timezone.utc


@dataclass(frozen=True)
class EconomicsPolicy:
    enabled: bool
    suppress_identical_prompts: bool
    dedupe_window_seconds: int
    max_prompt_tokens: int
    max_estimated_tokens: int
    max_iterations: int
    max_daily_runs: int
    max_daily_tokens: int
    max_run_usd: Decimal
    max_daily_usd: Decimal
    alert_after_noop: int


@dataclass(frozen=True)
class Admission:
    allowed: bool
    prompt_fingerprint: str
    estimated_prompt_tokens: int
    estimated_tokens: int
    effective_max_iterations: int
    reason: Optional[str] = None
    alert: bool = False
    alert_reasons: tuple[str, ...] = ()


def _coerce_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _coerce_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return max(Decimal("0"), result)


def policy_from_config(config: Optional[dict[str, Any]], job: Optional[dict[str, Any]] = None) -> EconomicsPolicy:
    """Resolve the global cron economics policy with an optional job override."""
    raw: dict[str, Any] = dict(DEFAULT_POLICY)
    cron = config.get("cron") if isinstance(config, dict) else None
    configured = cron.get("economics") if isinstance(cron, dict) else None
    if isinstance(configured, dict):
        raw.update(configured)
    job_policy = (job or {}).get("economics") or (job or {}).get("budget")
    if isinstance(job_policy, dict):
        raw.update(job_policy)
    return EconomicsPolicy(
        enabled=bool(raw.get("enabled", True)),
        suppress_identical_prompts=bool(raw.get("suppress_identical_prompts", True)),
        dedupe_window_seconds=_coerce_int(raw.get("dedupe_window_seconds"), 86400, minimum=0),
        max_prompt_tokens=_coerce_int(raw.get("max_prompt_tokens"), 128000, minimum=1),
        max_estimated_tokens=_coerce_int(raw.get("max_estimated_tokens"), 256000, minimum=1),
        max_iterations=_coerce_int(raw.get("max_iterations"), 12, minimum=1),
        max_daily_runs=_coerce_int(raw.get("max_daily_runs"), 24, minimum=0),
        max_daily_tokens=_coerce_int(raw.get("max_daily_tokens"), 1000000, minimum=0),
        max_run_usd=_coerce_decimal(raw.get("max_run_usd"), Decimal("1.00")),
        max_daily_usd=_coerce_decimal(raw.get("max_daily_usd"), Decimal("5.00")),
        alert_after_noop=_coerce_int(raw.get("alert_after_noop"), 3, minimum=1),
    )


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def estimate_prompt_tokens(prompt: str) -> int:
    """Cheap upper-bound-ish estimate used before tokenizer/provider startup."""
    return max(1, (len(prompt or "") + 3) // 4)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(_UTC) if parsed.tzinfo else parsed.replace(tzinfo=_UTC)


def read_recent_audit(path: Path, *, job_id: str, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Read only recent, valid records for one job; malformed lines fail open."""
    if not path.exists():
        return []
    current = (now or datetime.now(_UTC)).astimezone(_UTC)
    cutoff = current - timedelta(days=1)
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-5000:]
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict) or str(record.get("job_id") or "") != str(job_id):
            continue
        timestamp = _parse_ts(record.get("ts"))
        if timestamp is not None and timestamp >= cutoff:
            records.append(record)
    return records


def _decimal_record(record: dict[str, Any], key: str) -> Decimal:
    return _coerce_decimal(record.get(key), Decimal("0"))


def admit(
    *,
    job: dict[str, Any],
    prompt: str,
    model: str,
    provider: str,
    config: Optional[dict[str, Any]],
    current_max_iterations: int,
    audit_path: Path,
    now: Optional[datetime] = None,
) -> Admission:
    """Decide whether a scheduled LLM run may start.

    ``model`` and ``provider`` are intentionally accepted even though this
    first gate uses token budgets rather than live provider billing. They are
    recorded by the caller and leave a stable seam for provider-price
    reconciliation later.
    """
    del model, provider  # retained in the API for the audit/reconciliation seam
    policy = policy_from_config(config, job)
    fingerprint = prompt_fingerprint(prompt)
    prompt_tokens = estimate_prompt_tokens(prompt)
    effective_iterations = min(max(1, int(current_max_iterations)), policy.max_iterations)
    estimated_tokens = prompt_tokens * effective_iterations
    if not policy.enabled:
        return Admission(True, fingerprint, prompt_tokens, estimated_tokens, effective_iterations)

    records = read_recent_audit(audit_path, job_id=str(job.get("id") or ""), now=now)
    completed = [r for r in records if r.get("status") in {"completed", "started"}]
    daily_tokens = sum(int(r.get("total_tokens") or 0) for r in completed)
    daily_cost = sum((_decimal_record(r, "cost_usd") for r in completed), Decimal("0"))

    reasons: list[str] = []
    if prompt_tokens > policy.max_prompt_tokens:
        reasons.append(
            f"expanded prompt is about {prompt_tokens:,} tokens, above the "
            f"{policy.max_prompt_tokens:,}-token admission limit"
        )
    if estimated_tokens > policy.max_estimated_tokens:
        reasons.append(
            f"worst-case run is about {estimated_tokens:,} tokens, above the "
            f"{policy.max_estimated_tokens:,}-token admission limit"
        )
    if policy.max_daily_runs and len(completed) >= policy.max_daily_runs:
        reasons.append(f"daily run limit reached ({policy.max_daily_runs})")
    if policy.max_daily_tokens and daily_tokens >= policy.max_daily_tokens:
        reasons.append(f"daily token limit reached ({policy.max_daily_tokens:,})")
    if policy.max_daily_usd and daily_cost >= policy.max_daily_usd:
        reasons.append(f"daily budget reached (${policy.max_daily_usd})")

    duplicate_streak = 0
    previous_duplicate_alert = False
    # A static prompt can still be a valid daily job (for example, a report
    # whose changing inputs are discovered through terminal tools). Exact
    # prompt dedupe is safe only when the job declares a cheap state source;
    # otherwise the hard run/token budgets remain the protection.
    stateful_job = bool(
        job.get("script")
        or job.get("monitor_script")
        or job.get("monitor_url")
        or job.get("context_from")
        or (isinstance(job.get("economics"), dict) and job["economics"].get("dedupe_static_prompt"))
    )
    if policy.suppress_identical_prompts and stateful_job and records:
        current = (now or datetime.now(_UTC)).astimezone(_UTC)
        for record in reversed(records):
            if record.get("prompt_fingerprint") != fingerprint:
                break
            timestamp = _parse_ts(record.get("ts"))
            if timestamp and policy.dedupe_window_seconds:
                if (current - timestamp).total_seconds() > policy.dedupe_window_seconds:
                    break
            if record.get("outcome") in {"completed", "no_change", "skipped"}:
                duplicate_streak += 1
                previous_duplicate_alert = previous_duplicate_alert or bool(record.get("economics_alert"))
        if duplicate_streak:
            reasons.append(
                f"identical expanded prompt repeated {duplicate_streak + 1} times "
                f"within {policy.dedupe_window_seconds}s"
            )

    if not reasons:
        return Admission(True, fingerprint, prompt_tokens, estimated_tokens, effective_iterations)

    duplicate_only = all(reason.startswith("identical expanded prompt") for reason in reasons)
    reason_text = "; ".join(reasons)
    already_alerted = any(
        record.get("economics_alert")
        and str(record.get("economics_reason") or "") == reason_text
        for record in records
    )
    alert = (
        duplicate_only
        and duplicate_streak + 1 >= policy.alert_after_noop
        and not previous_duplicate_alert
    ) or (not duplicate_only and not already_alerted)
    return Admission(
        False,
        fingerprint,
        prompt_tokens,
        estimated_tokens,
        effective_iterations,
        reason=reason_text,
        alert=alert,
        alert_reasons=tuple(reasons),
    )


def cost_tripwire(*, total_tokens: int, cost_usd: Optional[Any], config: Optional[dict[str, Any]], job: dict[str, Any]) -> tuple[str, ...]:
    """Return post-run violations for the durable audit/alert path."""
    policy = policy_from_config(config, job)
    reasons: list[str] = []
    if total_tokens > policy.max_estimated_tokens:
        reasons.append(f"actual run used {total_tokens:,} tokens")
    if cost_usd is not None:
        actual = _coerce_decimal(cost_usd, Decimal("0"))
        if policy.max_run_usd and actual > policy.max_run_usd:
            reasons.append(f"actual run cost ${actual} exceeded ${policy.max_run_usd}")
    return tuple(reasons)
