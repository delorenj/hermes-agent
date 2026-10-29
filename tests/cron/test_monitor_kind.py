"""Tests for monitor-mode cron jobs — cheap source each tick, hash-suppressed agent runs.

A monitor job runs a cheap *monitor source* (``monitor_script`` or
``monitor_url``) on every tick, hashes the exact output bytes, and:

* unchanged output  → suppressed run: NO agent invocation, NO delivery,
  visible in the executions ledger as a silent no-change tick;
* changed output    → a "MONITOR CHANGE DETECTED" block (unified diff of
  old vs new, capped, plus the new output) is injected into the prompt and
  the agent runs normally;
* first run         → always runs the agent (nothing to compare against);
* source failure    → treated as an ERROR (alert delivered), never as a
  change — and the stored hash is NOT updated.

State (`monitor_state.last_output_hash` / `last_changed_at`) lives on the
job record in jobs.json plus hash-addressed immutable snapshots. Only terminal
scheduler semantic success acknowledges; run_job alone is provisional.

Inspired by: ChatGPT Work monitor tasks (idea-level, docs-only);
enabler: #80774.
"""

from __future__ import annotations

import json
import os
import sys

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts/snapshots don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))
    from tools.environments import local
    monkeypatch.setattr(local, "build_subprocess_env", lambda **_kwargs: {
        "PATH": os.environ["PATH"], "HERMES_HOME": str(home),
    })

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.monitor
    importlib.reload(cron.monitor)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


def _write_script(home, name: str, body: str) -> str:
    path = home / "scripts" / name
    path.write_text(body, encoding="utf-8")
    return name


def _run_terminal_job(job):
    """Exercise the real scheduler terminal boundary while returning its inner result for assertions."""
    from unittest.mock import patch
    from cron import scheduler

    results = []
    real = scheduler.run_job
    def capture(*args, **kwargs):
        result = real(*args, **kwargs)
        results.append(result)
        return result
    with patch.object(scheduler, "run_job", capture):
        assert scheduler.run_one_job(job)
    return results[0]


def _install_agent_stubs(monkeypatch, observed: dict):
    """Stub the agent machinery so run_job's LLM path executes without creds.

    ``observed["prompts"]`` collects the prompt each agent run received;
    ``observed["agent_runs"]`` counts real agent invocations.
    """
    import cron.scheduler as sched
    from cron import scheduler_delivery as sched_delivery

    observed.setdefault("prompts", [])
    observed.setdefault("agent_runs", 0)

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, prompt, *_a, **_kw):
            observed["agent_runs"] += 1
            observed["prompts"].append(prompt)
            return {"final_response": "agent done", "messages": []}

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

    fake_mod = type(sys)("run_agent")
    fake_mod.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

    from hermes_cli import runtime_provider as _rtp
    monkeypatch.setattr(
        _rtp,
        "resolve_runtime_provider",
        lambda **_kw: {
            "provider": "test",
            "api_key": "k",
            "base_url": "http://test.local",
            "api_mode": "chat_completions",
        },
    )

    monkeypatch.setattr(sched_delivery, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)


# ---------------------------------------------------------------------------
# create_job: data-layer semantics for monitor fields
# ---------------------------------------------------------------------------




def test_create_job_monitor_script_and_url_mutually_exclusive(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="monitor_script and monitor_url"):
        create_job(
            prompt="p",
            schedule="every 5m",
            monitor_script="mon.sh",
            monitor_url="https://example.com/status",
        )


def test_create_job_monitor_rejected_with_no_agent(hermes_env):
    from cron.jobs import create_job

    _write_script(hermes_env, "w.sh", "echo hi\n")
    with pytest.raises(ValueError, match="no_agent"):
        create_job(
            prompt=None,
            schedule="every 5m",
            script="w.sh",
            no_agent=True,
            monitor_script="w.sh",
        )


def test_update_job_rejects_no_agent_on_monitor_job(hermes_env):
    """The create-time monitor×no_agent invariant must hold through the
    update door too — the scheduler's no_agent short-circuit runs before
    the monitor gate, so flipping no_agent=True on a monitor job would
    silently disable the monitor (post-merge audit of #81138)."""
    from cron.jobs import create_job, update_job

    _write_script(hermes_env, "mon.sh", "echo stable\n")
    _write_script(hermes_env, "w.sh", "echo hi\n")
    job = create_job(
        prompt="React to the change",
        schedule="every 5m",
        monitor_script="mon.sh",
        deliver="local",
    )
    with pytest.raises(ValueError, match="no_agent"):
        update_job(job["id"], {"no_agent": True, "script": "w.sh"})


def test_update_job_rejects_adding_monitor_to_no_agent_job(hermes_env):
    from cron.jobs import create_job, update_job

    _write_script(hermes_env, "w.sh", "echo hi\n")
    _write_script(hermes_env, "mon.sh", "echo stable\n")
    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="w.sh",
        no_agent=True,
        deliver="local",
    )
    with pytest.raises(ValueError, match="no_agent"):
        update_job(job["id"], {"monitor_script": "mon.sh"})


def test_update_job_rejects_second_monitor_source(hermes_env):
    from cron.jobs import create_job, update_job

    _write_script(hermes_env, "mon.sh", "echo stable\n")
    job = create_job(
        prompt="React",
        schedule="every 5m",
        monitor_script="mon.sh",
        deliver="local",
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        update_job(job["id"], {"monitor_url": "https://example.com/status"})


def test_update_job_allows_clearing_monitor_then_no_agent(hermes_env):
    """Clearing the monitor and flipping no_agent in ONE update is valid —
    the invariant is checked on the merged record, not per-field."""
    from cron.jobs import create_job, get_job, update_job

    _write_script(hermes_env, "mon.sh", "echo stable\n")
    _write_script(hermes_env, "w.sh", "echo hi\n")
    job = create_job(
        prompt="React",
        schedule="every 5m",
        monitor_script="mon.sh",
        deliver="local",
    )
    update_job(job["id"], {"monitor_script": "", "no_agent": True, "script": "w.sh"})
    reloaded = get_job(job["id"])
    assert reloaded.get("monitor_script") is None
    assert reloaded["no_agent"] is True




# ---------------------------------------------------------------------------
# cron.monitor: hashing + diff unit behavior
# ---------------------------------------------------------------------------


def test_hash_is_exact_bytes(hermes_env):
    from cron.monitor import hash_monitor_output

    assert hash_monitor_output("a\nb") == hash_monitor_output("a\nb")
    # Exact-bytes contract: even whitespace-only differences are changes.
    assert hash_monitor_output("a\nb") != hash_monitor_output("a\nb ")


def test_unified_diff_is_capped(hermes_env):
    from cron.monitor import MAX_DIFF_CHARS, build_monitor_diff

    old = "\n".join(f"line {i}" for i in range(5000))
    new = "\n".join(f"LINE {i}" for i in range(5000))
    diff = build_monitor_diff(old, new)
    assert len(diff) <= MAX_DIFF_CHARS + 200  # cap + truncation notice
    assert "truncated" in diff


# ---------------------------------------------------------------------------
# scheduler.run_job: monitor gate behavior
# ---------------------------------------------------------------------------


def _make_monitor_job(hermes_env, script_body: str):
    from cron.jobs import create_job

    _write_script(hermes_env, "mon.sh", script_body)
    return create_job(
        prompt="Summarize what changed",
        schedule="every 5m",
        monitor_script="mon.sh",
        deliver="local",
    )


def test_first_run_always_runs_agent(hermes_env, monkeypatch):
    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    success, _doc, _final, error = _run_terminal_job(job)
    assert success is True
    assert error is None
    assert observed["agent_runs"] == 1
    # First run: new output is injected as monitor context.
    assert "state A" in observed["prompts"][0]


def test_bidi_monitor_output_is_sanitized_before_agent(hermes_env, monkeypatch):
    """Runtime monitor data with a WhatsApp-style bidi marker must not block the job (#111523)."""

    job = _make_monitor_job(hermes_env, "printf 'Alice\\342\\200\\252 Work\\n'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    success, _, _, error = _run_terminal_job(job)

    assert success is True
    assert error is None
    assert observed["agent_runs"] == 1
    assert "\u202a" not in observed["prompts"][0]
    assert "Alice Work" in observed["prompts"][0]


def test_monitor_script_uses_configured_interpreter(hermes_env, monkeypatch):
    """A monitor script uses the same job-level interpreter as `script`."""
    import stat

    from cron.jobs import create_job

    wrapper = hermes_env / "venv" / "bin" / "python3"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "env = os.environ.copy()\n"
        'env["CRON_MONITOR_WRAPPER_USED"] = "1"\n'
        "os.execve(sys.executable, [sys.executable, *sys.argv[1:]], env)\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    _write_script(
        hermes_env,
        "mon.py",
        'import os\nprint(os.environ.get("CRON_MONITOR_WRAPPER_USED", "0"))\n',
    )
    job = create_job(
        prompt="React to the change",
        schedule="every 5m",
        monitor_script="mon.py",
        interpreter=str(wrapper),
        deliver="local",
    )
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    success, _, _, error = _run_terminal_job(job)

    assert success is True
    assert error is None
    assert "1" in observed["prompts"][0]


def test_unchanged_output_suppresses_agent_run(hermes_env, monkeypatch):
    from cron.jobs import get_job
    from cron.scheduler import SILENT_MARKER

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    _run_terminal_job(job)
    assert observed["agent_runs"] == 1

    # Second tick with identical output → suppressed: no agent, silent.
    job = get_job(job["id"])
    success, doc, final, error = _run_terminal_job(job)
    assert success is True
    assert error is None
    assert final == SILENT_MARKER
    assert observed["agent_runs"] == 1  # unchanged — agent NOT re-invoked
    assert "no_change" in doc


def test_changed_output_injects_diff(hermes_env, monkeypatch):
    from cron.jobs import get_job

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    _run_terminal_job(job)

    # Mutate the monitored source, then fire again.
    _write_script(hermes_env, "mon.sh", "echo 'state B'\n")
    job = get_job(job["id"])
    success, _doc, _final, _error = _run_terminal_job(job)
    assert success is True
    assert observed["agent_runs"] == 2
    prompt = observed["prompts"][1]
    assert "-state A" in prompt
    assert "+state B" in prompt
    assert "state B" in prompt  # new output included verbatim


def test_hash_persists_across_scheduler_restart(hermes_env, monkeypatch):
    """Suppression state must survive a scheduler restart (module reload)."""
    import importlib

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    _run_terminal_job(job)
    assert observed["agent_runs"] == 1

    # Simulate restart: reload the cron modules, dropping in-memory state.
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.monitor
    importlib.reload(cron.monitor)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    _install_agent_stubs(monkeypatch, observed)

    job = cron.jobs.get_job(job["id"])
    assert job["monitor_state"]["last_output_hash"]
    success, _doc, final, _error = _run_terminal_job(job)
    assert success is True
    assert final == cron.scheduler.SILENT_MARKER
    assert observed["agent_runs"] == 1  # still suppressed after restart


def test_monitor_script_failure_is_error_not_change(hermes_env, monkeypatch):
    from cron.jobs import get_job

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)

    _run_terminal_job(job)
    stored_hash = get_job(job["id"])["monitor_state"]["last_output_hash"]

    # Break the source: non-zero exit must be an error, never a "change".
    _write_script(hermes_env, "mon.sh", "echo boom >&2\nexit 3\n")
    job = get_job(job["id"])
    success, _doc, _final, error = _run_terminal_job(job)
    assert success is False
    assert error is not None
    assert observed["agent_runs"] == 1  # agent NOT invoked on source failure
    # Stored hash untouched — a later recovery to 'state A' still suppresses.
    assert get_job(job["id"])["monitor_state"]["last_output_hash"] == stored_hash


def test_refused_monitor_run_retries_and_acknowledges_exact_snapshot(hermes_env, monkeypatch):
    from cron import economics, monitor
    from cron.jobs import _job_output_dir, get_job
    from cron.scheduler import SILENT_MARKER

    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    assert _run_terminal_job(job)[0]
    snapshot = _job_output_dir(job["id"]) / "monitor_last_output.txt"
    assert snapshot.read_text() == "state A"

    _write_script(hermes_env, "mon.sh", "echo 'state B'\n")
    original_admit = economics.admit
    def refuse_once(**kwargs):
        monkeypatch.setattr(economics, "admit", original_admit)
        return economics.Admission(False, "refused", 5, 60, 12, reason="daily run limit reached (24)")
    monkeypatch.setattr(economics, "admit", refuse_once)
    assert _run_terminal_job(get_job(job["id"]))[2] == SILENT_MARKER
    state = get_job(job["id"])["monitor_state"]
    assert state["last_output_hash"] == monitor.hash_monitor_output("state A")
    assert state["pending_output_hash"] == monitor.hash_monitor_output("state B")
    assert snapshot.read_text() == "state A"
    assert observed["agent_runs"] == 1

    # A refused audit must not itself poison the next identical expanded prompt.
    assert _run_terminal_job(get_job(job["id"]))[0]
    assert observed["agent_runs"] == 2
    assert "-state A" in observed["prompts"][-1]
    assert "+state B" in observed["prompts"][-1]
    assert snapshot.read_text() == "state B"
    assert "pending_output_hash" not in get_job(job["id"])["monitor_state"]
    success, doc, final, _ = _run_terminal_job(get_job(job["id"]))
    assert success and final == SILENT_MARKER and "no_change" in doc
    assert observed["agent_runs"] == 2


def test_failure_before_agent_retains_pending_monitor(hermes_env, monkeypatch):
    from cron import monitor, scheduler
    from cron.jobs import get_job

    job = _make_monitor_job(hermes_env, "echo pending\n")
    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    original = scheduler._load_cron_job_config
    def fail(*_args):
        raise RuntimeError("configuration unavailable")
    monkeypatch.setattr(scheduler, "_load_cron_job_config", fail)
    assert _run_terminal_job(job)[0] is False
    assert observed["agent_runs"] == 0
    state = get_job(job["id"])["monitor_state"]
    assert state["pending_output_hash"] == monitor.hash_monitor_output("pending")
    assert state.get("last_output_hash") is None
    monkeypatch.setattr(scheduler, "_load_cron_job_config", original)
    assert _run_terminal_job(get_job(job["id"]))[0]
    assert observed["agent_runs"] == 1


def test_newer_observation_cannot_be_acknowledged_by_older_run(hermes_env, monkeypatch):
    from cron import monitor
    from cron.jobs import _job_output_dir, get_job

    job = _make_monitor_job(hermes_env, "echo A\n")
    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    assert _run_terminal_job(job)[0]
    snapshot = _job_output_dir(job["id"]) / "monitor_last_output.txt"
    _write_script(hermes_env, "mon.sh", "echo B\n")
    from run_agent import AIAgent
    original = AIAgent.run_conversation
    def observe_newer(self, prompt, *args, **kwargs):
        _write_script(hermes_env, "mon.sh", "echo C\n")
        newer = monitor.check_monitor(get_job(job["id"]))
        assert newer.changed and newer.output == "C"
        monkeypatch.setattr(AIAgent, "run_conversation", original)
        return original(self, prompt, *args, **kwargs)
    monkeypatch.setattr(AIAgent, "run_conversation", observe_newer)
    assert _run_terminal_job(get_job(job["id"]))[0]
    assert snapshot.read_text() == "A"
    assert get_job(job["id"])["monitor_state"]["pending_output_hash"] == monitor.hash_monitor_output("C")
    assert _run_terminal_job(get_job(job["id"]))[0]
    assert snapshot.read_text() == "C"
    assert "-A" in observed["prompts"][-1] and "+C" in observed["prompts"][-1]


def test_monitor_source_failure_does_not_ack_pending(hermes_env, monkeypatch):
    from cron import monitor
    from cron.jobs import get_job
    job = _make_monitor_job(hermes_env, "echo pending\n")
    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    from cron import economics
    monkeypatch.setattr(economics, "admit", lambda **kw: economics.Admission(
        False, "refused", 5, 60, 12, reason="daily run limit reached"))
    _run_terminal_job(job)
    _write_script(hermes_env, "mon.sh", "exit 3\n")
    assert _run_terminal_job(get_job(job["id"]))[0] is False
    assert get_job(job["id"])["monitor_state"]["pending_output_hash"] == monitor.hash_monitor_output("pending")


# ---------------------------------------------------------------------------
# cronjob tool: API-layer wiring
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_with_monitor_script(hermes_env):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    _write_script(hermes_env, "mon.sh", "echo hi\n")
    result = json.loads(
        cronjob(
            action="create",
            prompt="React",
            schedule="every 5m",
            monitor_script="mon.sh",
            deliver="local",
        )
    )
    assert result.get("success") is True
    job = get_job(result["job_id"])
    assert job["monitor_script"] == "mon.sh"


def test_cronjob_tool_rejects_monitor_script_path_escape(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(
            action="create",
            prompt="React",
            schedule="every 5m",
            monitor_script="../evil.sh",
            deliver="local",
        )
    )
    assert result.get("success") is False


def test_cronjob_tool_update_clears_monitor_script(hermes_env):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    _write_script(hermes_env, "mon.sh", "echo hi\n")
    created = json.loads(
        cronjob(
            action="create",
            prompt="React",
            schedule="every 5m",
            monitor_script="mon.sh",
            deliver="local",
        )
    )
    result = json.loads(
        cronjob(action="update", job_id=created["job_id"], monitor_script="")
    )
    assert result.get("success") is True
    assert get_job(created["job_id"]).get("monitor_script") is None
