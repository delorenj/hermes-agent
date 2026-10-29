"""Accepted regressions from the independent monitor-ack probes.

Monitors are latest-observation-wins state, not transition queues. Acknowledgement is
terminal semantic execution success, independent of notification transport success.
"""
import importlib
import threading

import pytest

from tests.cron.test_monitor_kind import (
    hermes_env, _install_agent_stubs, _make_monitor_job, _run_terminal_job, _write_script,
)


@pytest.fixture
def running_monitor(hermes_env, monkeypatch):
    from cron import scheduler
    job = _make_monitor_job(hermes_env, "echo A\n")
    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda _job: False)
    _run_terminal_job(job)
    return job["id"], observed


def _assert_pending(job_id):
    from cron import monitor
    from cron.jobs import get_job, _job_output_dir
    state = get_job(job_id)["monitor_state"]
    assert state["last_output_hash"] == monitor.hash_monitor_output("A")
    assert state["pending_output_hash"] == monitor.hash_monitor_output("B")
    assert (_job_output_dir(job_id) / "monitor_last_output.txt").read_text() == "A"


def test_aba_latest_observation_supersedes_inflight_b(running_monitor, hermes_env, monkeypatch):
    from cron import monitor, scheduler
    from cron.jobs import get_job, _job_output_dir
    from run_agent import AIAgent
    job_id, observed = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    original = AIAgent.run_conversation
    def during_b(self, prompt, *args, **kwargs):
        _write_script(hermes_env, "mon.sh", "echo A\n")
        assert not monitor.check_monitor(get_job(job_id)).changed
        return original(self, prompt, *args, **kwargs)
    monkeypatch.setattr(AIAgent, "run_conversation", during_b)
    _run_terminal_job(get_job(job_id))
    state = get_job(job_id)["monitor_state"]
    assert state["last_output_hash"] == monitor.hash_monitor_output("A")
    assert "pending_output_hash" not in state
    assert (_job_output_dir(job_id) / "monitor_last_output.txt").read_text() == "A"
    assert _run_terminal_job(get_job(job_id))[2] == scheduler.SILENT_MARKER
    assert observed["agent_runs"] == 2
    # Returning to A loses no latest-state attention. A subsequent C is still processed.
    monkeypatch.setattr(AIAgent, "run_conversation", original)
    _write_script(hermes_env, "mon.sh", "echo C\n")
    _run_terminal_job(get_job(job_id))
    assert observed["agent_runs"] == 3
    assert "-A" in observed["prompts"][-1] and "+C" in observed["prompts"][-1]
    assert get_job(job_id)["monitor_state"]["last_output_hash"] == monitor.hash_monitor_output("C")


@pytest.mark.parametrize("response", ["[CRON_FAILURE]\nDelegated child failed", "", " \n "])
def test_semantic_failure_retains_pending(running_monitor, hermes_env, monkeypatch, response):
    from cron import scheduler
    from cron.jobs import get_job
    from cron.executions import get_execution
    from run_agent import AIAgent
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    original = AIAgent.run_conversation
    monkeypatch.setattr(AIAgent, "run_conversation", lambda *_a, **_kw: {
        "final_response": response, "messages": [],
    })
    job = get_job(job_id)
    assert scheduler.run_one_job(job)
    assert get_execution(job["execution_id"])["status"] == "failed"
    assert get_job(job_id)["last_status"] == "error"
    _assert_pending(job_id)
    # A later bounded tick uses fresh prompt context, not a cumulative conversation retry.
    monkeypatch.setattr(AIAgent, "run_conversation", original)
    _run_terminal_job(get_job(job_id))
    assert "pending_output_hash" not in get_job(job_id)["monitor_state"]


def test_run_job_is_provisional_not_an_ack(running_monitor, hermes_env):
    from cron import scheduler
    from cron.jobs import get_job
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    assert scheduler.run_job(get_job(job_id))[0]
    _assert_pending(job_id)


@pytest.mark.parametrize("response", ["processed", "[SILENT]"])
@pytest.mark.parametrize("delivery_error", [None, "channel offline"])
def test_terminal_success_acks_even_when_delivery_fails(
    running_monitor, hermes_env, monkeypatch, response, delivery_error,
):
    from cron import monitor, scheduler
    from cron.jobs import get_job, claim_job_for_fire
    from cron.executions import get_execution
    from run_agent import AIAgent
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    monkeypatch.setattr(AIAgent, "run_conversation", lambda *_a, **_kw: {"final_response": response})
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_a, **_kw: delivery_error)
    assert claim_job_for_fire(job_id)
    job = get_job(job_id)
    assert scheduler.run_one_job(job)
    state = get_job(job_id)["monitor_state"]
    assert state["last_output_hash"] == monitor.hash_monitor_output("B")
    assert "pending_output_hash" not in state
    assert get_execution(job["execution_id"])["status"] == "completed"
    assert _run_terminal_job(get_job(job_id))[2] == scheduler.SILENT_MARKER


@pytest.mark.parametrize("boundary", ["agent-return", "delivery", "terminal-write"])
def test_lost_owner_cannot_ack(running_monitor, hermes_env, monkeypatch, boundary):
    from cron import scheduler
    from cron.jobs import get_job, claim_job_for_fire, _with_job, save_jobs
    from run_agent import AIAgent
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    assert claim_job_for_fire(job_id)
    job = get_job(job_id)
    def steal():
        def replace(rows, _i, stored):
            stored["fire_claim"] = {**stored["fire_claim"], "by": "replacement-owner"}
            save_jobs(rows)
        _with_job(job_id, replace)
    if boundary == "agent-return":
        original = AIAgent.run_conversation
        def run(*args, **kwargs):
            result = original(*args, **kwargs)
            steal()
            return result
        monkeypatch.setattr(AIAgent, "run_conversation", run)
    elif boundary == "delivery":
        monkeypatch.setattr(scheduler, "_deliver_result", lambda *_a, **_kw: steal())
    else:
        original = scheduler.mark_job_run
        def mark(*args, **kwargs):
            steal()
            return original(*args, **kwargs)
        monkeypatch.setattr(scheduler, "mark_job_run", mark)
    assert scheduler.run_one_job(job)
    _assert_pending(job_id)
    assert get_job(job_id)["fire_claim"]["by"] == "replacement-owner"


@pytest.mark.parametrize("kind", ["transport", "shutdown"])
def test_interrupted_after_agent_return_cannot_ack(running_monitor, hermes_env, monkeypatch, kind):
    from cron import scheduler
    from cron.jobs import get_job, claim_job_for_fire
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    assert claim_job_for_fire(job_id)
    job = get_job(job_id)
    cancel = threading.Event()
    def interrupt(*_a, **_kw):
        if kind == "transport":
            cancel.set()
        else:
            scheduler._interrupted_job_ids.add(scheduler._inflight_key(job_id))
    monkeypatch.setattr(scheduler, "_deliver_result", interrupt)
    assert scheduler.run_one_job(job, cancel_event=cancel)
    _assert_pending(job_id)


@pytest.mark.parametrize("failure", ["jobs", "ledger-error", "ledger-cas"])
def test_terminal_persistence_failure_cannot_ack(running_monitor, hermes_env, monkeypatch, failure):
    from cron import scheduler
    from cron.jobs import get_job
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    def fail(*_a, **_kw):
        raise OSError("injected terminal persistence failure")
    if failure == "jobs":
        monkeypatch.setattr(scheduler, "mark_job_run", fail)
    else:
        monkeypatch.setattr(scheduler, "finish_execution", fail if failure == "ledger-error" else lambda *_a, **_kw: None)
    scheduler.run_one_job(get_job(job_id))
    _assert_pending(job_id)


def test_failed_snapshot_hash_commit_keeps_a_and_diff_on_retry(running_monitor, hermes_env, monkeypatch):
    from cron import monitor, jobs
    job_id, observed = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    original = jobs.save_jobs
    b_hash = monitor.hash_monitor_output("B")
    def fail_at_ack(rows, **kwargs):
        if any(row.get("monitor_state", {}).get("last_output_hash") == b_hash for row in rows):
            raise OSError("injected disk full at acknowledgement")
        return original(rows, **kwargs)
    monkeypatch.setattr(jobs, "save_jobs", fail_at_ack)
    _run_terminal_job(jobs.get_job(job_id))
    _assert_pending(job_id)
    assert monitor._immutable_snapshot_path(job_id, b_hash).read_text() == "B"
    monkeypatch.setattr(jobs, "save_jobs", original)
    _run_terminal_job(jobs.get_job(job_id))
    assert "-A" in observed["prompts"][-1] and "+B" in observed["prompts"][-1]
    assert observed["agent_runs"] == 3
    assert monitor._read_last_output(job_id, monitor.hash_monitor_output("A")) == "A"


@pytest.mark.parametrize("alias_state", ["stale", "missing"])
def test_committed_hash_recovers_alias_before_source_after_restart(
    running_monitor, hermes_env, monkeypatch, alias_state,
):
    from cron import monitor, jobs
    job_id, observed = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    original = monitor._repair_snapshot
    def crash_after_commit(job_id, output_hash):
        if output_hash == monitor.hash_monitor_output("B"):
            raise OSError("injected crash after hash commit before alias publication")
        return original(job_id, output_hash)
    monkeypatch.setattr(monitor, "_repair_snapshot", crash_after_commit)
    _run_terminal_job(jobs.get_job(job_id))
    alias = monitor._snapshot_path(job_id)
    assert alias.read_text() == "A"
    assert jobs.get_job(job_id)["monitor_state"]["last_output_hash"] == monitor.hash_monitor_output("B")
    if alias_state == "missing":
        alias.unlink()
    importlib.reload(monitor)
    _write_script(hermes_env, "mon.sh",
                  f'export ZCRANK_GATE_LAST="$HERMES_HOME/cron/output/{job_id}/monitor_last_output.txt"\n'
                  'test "$(cat "$ZCRANK_GATE_LAST")" = B || exit 5\nprintf B\n')
    real_source = monitor._run_monitor_source
    def source(job):
        # Same fixed-path consumer as ZCRANK_GATE_LAST; it must see B, not stale A.
        assert alias.read_text() == "B"
        return real_source(job)
    monkeypatch.setattr(monitor, "_run_monitor_source", source)
    assert not monitor.check_monitor(jobs.get_job(job_id)).changed
    assert observed["agent_runs"] == 2


def test_snapshot_directory_sync_failure_cannot_commit_hash(running_monitor, hermes_env, monkeypatch):
    from cron import monitor, jobs
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    def fail(_path):
        raise OSError("injected snapshot directory fsync failure")
    monkeypatch.setattr(monitor, "_fsync_snapshot_directory", fail)
    _run_terminal_job(jobs.get_job(job_id))
    _assert_pending(job_id)


def test_terminal_owner_lock_blocks_replacement_until_ack(running_monitor, hermes_env, monkeypatch):
    from cron import monitor, scheduler
    from cron.jobs import get_job, claim_job_for_fire
    job_id, _ = running_monitor
    _write_script(hermes_env, "mon.sh", "echo B\n")
    assert claim_job_for_fire(job_id)
    original = scheduler.finish_execution
    workers, acquired, attempted = [], threading.Event(), threading.Event()
    def finish(*args, **kwargs):
        receipt = original(*args, **kwargs)
        def replace():
            attempted.set()
            if claim_job_for_fire(job_id):
                acquired.set()
        worker = threading.Thread(target=replace)
        worker.start()
        workers.append(worker)
        assert attempted.wait(10)
        return receipt
    real_ack = monitor.acknowledge_monitor
    def ack(*args):
        assert not acquired.is_set(), "replacement owner entered between terminal retirement and ack"
        return real_ack(*args)
    monkeypatch.setattr(scheduler, "finish_execution", finish)
    monkeypatch.setattr(monitor, "acknowledge_monitor", ack)
    assert scheduler.run_one_job(get_job(job_id))
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()
    assert acquired.is_set()
    assert get_job(job_id)["monitor_state"]["last_output_hash"] == monitor.hash_monitor_output("B")


def test_legacy_fixed_snapshot_migrates_without_agent(running_monitor):
    from cron import monitor, jobs
    job_id, observed = running_monitor
    a_hash = monitor.hash_monitor_output("A")
    monitor._immutable_snapshot_path(job_id, a_hash).unlink()
    assert not monitor.check_monitor(jobs.get_job(job_id)).changed
    assert monitor._read_last_output(job_id, a_hash) == "A"
    assert observed["agent_runs"] == 1
