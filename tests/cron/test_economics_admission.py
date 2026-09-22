"""Unattended-run economics admission (fork: cron/economics.py + scheduler wiring).

A provider spend limit is necessary but not sufficient: an unattended job can stay under a daily ceiling
while producing no useful change across hundreds of identical fires. Admission decides before any
provider call, from the usage audit the scheduler already writes.
"""

import json
from datetime import datetime, timezone

from cron import economics
from cron.scheduler_preflight import ECONOMICS_SKIP_MARKER

JOB = {"id": "job1", "name": "Nightly", "script": "echo state"}


def _audit(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _admit(tmp_path, *, prompt="do the thing", job=JOB, config=None, iterations=90):
    return economics.admit(
        job=job, prompt=prompt, model="m", provider="p", config=config or {},
        current_max_iterations=iterations, audit_path=tmp_path / "usage_audit.jsonl")


def test_first_run_is_admitted_and_turns_are_capped(tmp_path):
    adm = _admit(tmp_path, iterations=10_000)
    assert adm.allowed
    assert adm.effective_max_iterations == economics.DEFAULT_POLICY["max_iterations"]


def test_unchanged_prompt_on_a_stateful_job_is_suppressed_before_inference(tmp_path):
    fp = economics.prompt_fingerprint("same prompt")
    now = datetime.now(timezone.utc).isoformat()
    _audit(tmp_path / "usage_audit.jsonl", [
        {"ts": now, "job_id": "job1", "status": "completed", "outcome": "completed",
         "prompt_fingerprint": fp, "total_tokens": 10},
    ])
    adm = _admit(tmp_path, prompt="same prompt")
    assert not adm.allowed
    assert adm.reason.startswith("identical expanded prompt")


def test_daily_run_cap_blocks_and_alerts_once(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"ts": now, "job_id": "job1", "status": "completed", "outcome": "completed",
         "prompt_fingerprint": f"fp{i}", "total_tokens": 1}
        for i in range(economics.DEFAULT_POLICY["max_daily_runs"])
    ]
    _audit(tmp_path / "usage_audit.jsonl", rows)
    first = _admit(tmp_path, job={"id": "job1"})
    assert not first.allowed and first.alert and "daily run limit" in first.reason

    rows.append({"ts": now, "job_id": "job1", "status": "skipped", "outcome": "blocked",
                 "economics_alert": True, "economics_reason": first.reason})
    _audit(tmp_path / "usage_audit.jsonl", rows)
    second = _admit(tmp_path, job={"id": "job1"})
    assert not second.allowed and not second.alert


def test_disabled_policy_admits_everything(tmp_path):
    adm = _admit(tmp_path, config={"cron": {"economics": {"enabled": False}}}, iterations=500)
    assert adm.allowed


def test_scheduler_skip_result_shapes(tmp_path, monkeypatch):
    from cron import scheduler

    rows = []
    monkeypatch.setattr(scheduler, "_write_usage_audit", rows.append)
    quiet = economics.Admission(False, "fp", 5, 50, 10, reason="identical expanded prompt repeated 2 times", alert=False)
    ok, doc, final, err = scheduler._economics_skip_result(quiet, JOB, "job1", "Nightly", "m", "p")
    assert ok is True and final == scheduler.SILENT_MARKER and err is None
    assert rows[-1]["outcome"] == "no_change" and rows[-1]["prompt_fingerprint"] == "fp"

    loud = economics.Admission(False, "fp", 5, 50, 10, reason="daily run limit reached (24)", alert=True)
    ok, doc, final, err = scheduler._economics_skip_result(loud, JOB, "job1", "Nightly", "m", "p")
    assert ok is False and final == "" and err.startswith(ECONOMICS_SKIP_MARKER)
    assert rows[-1]["outcome"] == "blocked" and rows[-1]["economics_alert"] is True


def test_economics_skip_is_delivered_once_as_a_guard_notice():
    from cron import scheduler

    content, blocked_config, silent, acked, incident = scheduler._compose_run_delivery(
        JOB, success=False, error=f"{ECONOMICS_SKIP_MARKER} daily run limit reached (24). No inference call was made.",
        final_response="", output_file=None)
    assert "economics guard" in content and "daily run limit reached" in content
    assert not blocked_config and not silent and not acked and incident is None
