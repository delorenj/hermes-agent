from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cron.economics import admit, estimate_prompt_tokens, policy_from_config, prompt_fingerprint


def _job() -> dict:
    return {"id": "abc123", "name": "test", "schedule": {"kind": "interval"}}


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_prompt_fingerprint_is_stable() -> None:
    assert prompt_fingerprint("same") == prompt_fingerprint("same")
    assert prompt_fingerprint("same") != prompt_fingerprint("changed")


def test_prompt_token_estimate_is_cheap_and_nonzero() -> None:
    assert estimate_prompt_tokens("") == 1
    assert estimate_prompt_tokens("1234") == 1
    assert estimate_prompt_tokens("12345") == 2


def test_defaults_bound_unattended_runs() -> None:
    policy = policy_from_config({})
    assert policy.enabled is True
    assert policy.suppress_identical_prompts is True
    assert policy.max_iterations == 12
    assert policy.max_prompt_tokens == 128000


def test_job_policy_overrides_global_policy() -> None:
    policy = policy_from_config(
        {"cron": {"economics": {"max_iterations": 4}}},
        {"economics": {"max_iterations": 2}},
    )
    assert policy.max_iterations == 2


def test_first_prompt_is_admitted_and_turns_are_capped(tmp_path: Path) -> None:
    decision = admit(
        job=_job(),
        prompt="small prompt",
        model="model",
        provider="openrouter",
        config={"cron": {"economics": {"max_iterations": 4}}},
        current_max_iterations=90,
        audit_path=tmp_path / "usage.jsonl",
    )
    assert decision.allowed is True
    assert decision.effective_max_iterations == 4


def test_identical_prompt_is_suppressed_after_success(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    _write(
        path,
        [{
            "ts": datetime.now(timezone.utc).isoformat(),
            "job_id": "abc123",
            "status": "completed",
            "outcome": "completed",
            "prompt_fingerprint": prompt_fingerprint("small prompt"),
            "total_tokens": 10,
            "cost_usd": "0.01",
        }],
    )
    job = _job()
    job["script"] = "state.sh"
    decision = admit(
        job=job,
        prompt="small prompt",
        model="model",
        provider="openrouter",
        config={},
        current_max_iterations=4,
        audit_path=path,
    )
    assert decision.allowed is False
    assert "identical expanded prompt" in (decision.reason or "")
    assert decision.alert is False


def test_static_prompt_is_not_deduped_without_state_source(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    _write(
        path,
        [{
            "ts": datetime.now(timezone.utc).isoformat(),
            "job_id": "abc123",
            "status": "completed",
            "outcome": "completed",
            "prompt_fingerprint": prompt_fingerprint("small prompt"),
            "total_tokens": 10,
            "cost_usd": "0.01",
        }],
    )
    decision = admit(
        job=_job(),
        prompt="small prompt",
        model="model",
        provider="openrouter",
        config={},
        current_max_iterations=4,
        audit_path=path,
    )
    assert decision.allowed is True


def test_third_identical_prompt_raises_tripwire(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    fingerprint = prompt_fingerprint("small prompt")
    records = []
    for _ in range(2):
        records.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "job_id": "abc123",
            "status": "skipped",
            "outcome": "no_change",
            "prompt_fingerprint": fingerprint,
            "economics_alert": False,
        })
    _write(path, records)
    job = _job()
    job["script"] = "state.sh"
    decision = admit(
        job=job,
        prompt="small prompt",
        model="model",
        provider="openrouter",
        config={},
        current_max_iterations=4,
        audit_path=path,
    )
    assert decision.allowed is False
    assert decision.alert is True


def test_prompt_cap_blocks_before_provider(tmp_path: Path) -> None:
    decision = admit(
        job=_job(),
        prompt="x" * 400,
        model="model",
        provider="openrouter",
        config={"cron": {"economics": {"max_prompt_tokens": 10}}},
        current_max_iterations=4,
        audit_path=tmp_path / "usage.jsonl",
    )
    assert decision.allowed is False
    assert "expanded prompt" in (decision.reason or "")


def test_daily_run_cap_blocks(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write(
        path,
        [
            {
                "ts": now,
                "job_id": "abc123",
                "status": "completed",
                "outcome": "completed",
                "prompt_fingerprint": f"other-{i}",
                "total_tokens": 10,
                "cost_usd": "0.01",
            }
            for i in range(2)
        ],
    )
    decision = admit(
        job=_job(),
        prompt="new prompt",
        model="model",
        provider="openrouter",
        config={"cron": {"economics": {"max_daily_runs": 2}}},
        current_max_iterations=4,
        audit_path=path,
    )
    assert decision.allowed is False
    assert "daily run limit" in (decision.reason or "")
