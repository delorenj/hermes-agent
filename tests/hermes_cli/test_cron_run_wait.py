"""``hermes cron run <job> --wait``: exit mapping, usage parse, timeout, and join-not-duplicate.

Real executions ledger and usage audit in a temp HERMES_HOME; the trigger is a stand-in for the
plain ``hermes cron run`` action (whose own behaviour is covered elsewhere).
"""
import argparse
import json
import threading
import time

import pytest

from hermes_cli import cron_wait


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "cron").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def job(home):
    from cron.jobs import create_job

    return create_job(prompt="x", schedule="every 5m", name="wait-me", scheduler="external")


def _audit(home, execution_id, prompt, completion, total=None):
    line = {"job_id": "j", "execution_id": execution_id, "prompt_tokens": prompt,
            "completion_tokens": completion, "total_tokens": total}
    with open(home / "cron" / "usage_audit.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _result(capsys):
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if ln.startswith(cron_wait.RESULT_PREFIX)][-1]
    return json.loads(line[len(cron_wait.RESULT_PREFIX):])


def _inline_trigger(job_id, *, success, error=None, usage=None, home=None):
    """What an inline ``hermes cron run`` does: claim an attempt, run, finish it."""
    from cron.executions import create_execution, finish_execution, mark_execution_running

    def trigger(_ref):
        rec = create_execution(job_id, source="direct")
        mark_execution_running(rec["id"])
        if usage:
            _audit(home, rec["id"], *usage)
        finish_execution(rec["id"], success=success, error=error)
        return 0

    return trigger


def test_success_exits_zero_and_reports_usage(home, job, capsys):
    rc = cron_wait.run_and_wait(
        job["id"], timeout=30, trigger=_inline_trigger(job["id"], success=True, usage=(1200, 345), home=home))
    assert rc == 0
    res = _result(capsys)
    assert res["status"] == "completed" and res["error"] is None
    assert (res["input_tokens"], res["output_tokens"], res["total_tokens"]) == (1200, 345, 1545)
    assert res["execution_id"]


def test_failure_exits_one_with_error(home, job, capsys):
    rc = cron_wait.run_and_wait(
        job["id"], timeout=30, trigger=_inline_trigger(job["id"], success=False, error="boom"))
    assert rc == 1
    res = _result(capsys)
    assert res["status"] == "failed" and res["error"] == "boom"
    assert res["input_tokens"] == 0 and res["output_tokens"] == 0  # no usage line -> 0


def test_usage_matches_only_this_attempt(home, job):
    _audit(home, "other-attempt", 9, 9)
    assert cron_wait.read_usage("mine") == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    _audit(home, "mine", 7, 3, total=11)
    assert cron_wait.read_usage("mine") == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 11}


def test_trigger_refusal_exits_one_without_waiting(home, job, capsys):
    rc = cron_wait.run_and_wait(job["id"], timeout=30, trigger=lambda _r: 1)
    assert rc == 1
    assert _result(capsys)["error"] == "trigger refused"


def test_unknown_job_exits_one(home, capsys):
    rc = cron_wait.run_and_wait("nope", timeout=30, trigger=lambda _r: 0)
    assert rc == 1
    assert "no job matches" in _result(capsys)["error"]


def test_timeout_exits_one_while_attempt_still_running(home, job, capsys):
    from cron.executions import create_execution, mark_execution_running

    def trigger(_ref):
        mark_execution_running(create_execution(job["id"], source="direct")["id"])
        return 0

    clock = {"t": 0.0}
    rc = cron_wait.run_and_wait(
        job["id"], timeout=5, trigger=trigger,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s), monotonic=lambda: clock["t"])
    assert rc == 1
    res = _result(capsys)
    assert "timed out" in res["error"] and res["execution_id"]


def test_second_trigger_joins_the_attempt_in_flight(home, job, capsys):
    """The claim CAS refuses a second attempt (trigger reports 'already firing' but rc 0); the waiter
    must follow the EXISTING attempt to its end rather than start or invent another."""
    from cron.executions import create_execution, finish_execution, list_executions, mark_execution_running

    first = create_execution(job["id"], source="scheduled")
    mark_execution_running(first["id"])

    def finish_soon():
        time.sleep(0.3)
        _audit(home, first["id"], 50, 5)
        finish_execution(first["id"], success=True)

    worker = threading.Thread(target=finish_soon)
    worker.start()
    rc = cron_wait.run_and_wait(job["id"], timeout=30, trigger=lambda _r: 0)
    worker.join()
    assert rc == 0
    res = _result(capsys)
    assert res["execution_id"] == first["id"]
    assert res["input_tokens"] == 50
    assert len(list_executions(job_id=job["id"])) == 1  # never a second attempt


def test_no_attempt_observed_exits_one(home, job, capsys, monkeypatch):
    monkeypatch.setattr(cron_wait, "_NO_ATTEMPT_GRACE_SECONDS", 3.0)
    clock = {"t": 0.0}
    rc = cron_wait.run_and_wait(
        job["id"], timeout=0, trigger=lambda _r: 0,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s), monotonic=lambda: clock["t"])
    assert rc == 1
    assert "no execution attempt" in _result(capsys)["error"]


def test_dead_owner_is_reported_not_waited_on(home, job, capsys, monkeypatch):
    from cron import executions
    from cron.executions import create_execution, mark_execution_running

    def trigger(_ref):
        mark_execution_running(create_execution(job["id"], source="direct")["id"])
        return 0

    monkeypatch.setattr(executions, "_owner_is_live", lambda pid, started: False)
    rc = cron_wait.run_and_wait(job["id"], timeout=30, trigger=trigger, sleep=lambda s: None)
    assert rc == 1
    assert "owner exited" in _result(capsys)["error"]


def test_cli_parses_wait_and_routes_to_run_and_wait(monkeypatch):
    from hermes_cli import cron as cron_cli
    from hermes_cli.subcommands.cron import build_cron_parser

    root = argparse.ArgumentParser()
    build_cron_parser(root.add_subparsers(dest="command"), cmd_cron=lambda a: 0)
    args = root.parse_args(["cron", "run", "abc123", "--wait", "--timeout", "90"])
    assert args.wait is True and args.timeout == 90.0

    seen = {}
    monkeypatch.setattr(cron_wait, "run_and_wait", lambda ref, **kw: seen.update(ref=ref, **kw) or 0)
    assert cron_cli.cron_command(args) == 0
    assert seen["ref"] == "abc123" and seen["timeout"] == 90.0

    plain = root.parse_args(["cron", "run", "abc123"])
    assert plain.wait is False
    monkeypatch.setattr(cron_cli, "_job_action", lambda *a: 7)
    assert cron_cli.cron_command(plain) == 7


def test_cli_scheduler_flag_on_create_and_edit():
    from hermes_cli.subcommands.cron import build_cron_parser

    root = argparse.ArgumentParser()
    build_cron_parser(root.add_subparsers(dest="command"), cmd_cron=lambda a: 0)
    assert root.parse_args(["cron", "create", "every 1h", "p", "--scheduler", "external"]).scheduler == "external"
    assert root.parse_args(["cron", "edit", "j1", "--scheduler", "internal"]).scheduler == "internal"
    with pytest.raises(SystemExit):
        root.parse_args(["cron", "create", "every 1h", "p", "--scheduler", "bogus"])
