"""Exec-boundary gateway isolation regressions.

The pytest monkeypatch guard is intentionally in-process.  These tests use
real child interpreters and disposable homes to prove that the path-valued
``HERMES_TEST_ISOLATION`` contract survives exec without touching a host
profile or signalling a host PID.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _child_env(*, operator_home: Path, isolation_home: Path) -> dict[str, str]:
    """Minimal marked child env with HERMES_HOME deliberately omitted."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(operator_home),
        "HERMES_TEST_ISOLATION": str(isolation_home),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "1",
    }


def _unmarked_env(*, operator_home: Path) -> dict[str, str]:
    """Minimal production-like env with all pytest isolation state removed."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(operator_home),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "1",
    }


def _run_self_restart_probe(
    *,
    operator_home: Path,
    isolation_home: Path,
    marked: bool,
) -> dict[str, object]:
    """Run the helper in a real child whose unmarked parent records SIGUSR1."""
    wrapper_script = textwrap.dedent(
        """
        import json
        import os
        import signal
        import subprocess
        import sys
        import time

        received_sigusr1 = False

        def record_sigusr1(signum, frame):
            global received_sigusr1
            received_sigusr1 = True

        signal.signal(signal.SIGUSR1, record_sigusr1)
        child_script = '''
        import json
        import os

        from hermes_cli.gateway import _request_gateway_self_restart

        result = _request_gateway_self_restart(int(os.environ["TARGET_PID"]))
        print(json.dumps({"result": result}))
        '''
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ["PROBE_OPERATOR_HOME"],
            "PYTHONPATH": os.environ["PROBE_REPO_ROOT"],
            "PYTHONUTF8": "1",
            "TARGET_PID": str(os.getpid()),
        }
        if os.environ["PROBE_MARKED"] == "1":
            child_env["HERMES_TEST_ISOLATION"] = os.environ["PROBE_ISOLATION_HOME"]

        child = subprocess.run(
            [sys.executable, "-c", child_script],
            cwd=os.environ["PROBE_REPO_ROOT"],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        time.sleep(0.05)
        print(json.dumps({
            "child_returncode": child.returncode,
            "child_stdout": child.stdout,
            "child_stderr": child.stderr,
            "received_sigusr1": received_sigusr1,
        }))
        """
    )
    wrapper_env = _unmarked_env(operator_home=operator_home)
    wrapper_env.update(
        {
            "PROBE_ISOLATION_HOME": str(isolation_home),
            "PROBE_MARKED": "1" if marked else "0",
            "PROBE_OPERATOR_HOME": str(operator_home),
            "PROBE_REPO_ROOT": str(REPO_ROOT),
        }
    )
    wrapper = subprocess.run(
        [sys.executable, "-c", wrapper_script],
        cwd=REPO_ROOT,
        env=wrapper_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert wrapper.returncode == 0, wrapper.stderr
    payload = json.loads(wrapper.stdout.strip().splitlines()[-1])
    assert payload["child_returncode"] == 0, payload["child_stderr"]
    child_payload = json.loads(str(payload["child_stdout"]).strip().splitlines()[-1])
    return {
        "helper_result": child_payload["result"],
        "received_sigusr1": payload["received_sigusr1"],
    }


def _run_graceful_restart_probe(
    *,
    operator_home: Path,
    isolation_home: Path,
    marked: bool,
) -> tuple[dict[str, object], bool]:
    """Run graceful restart against an unmarked, disposable process owner."""
    owner_script = textwrap.dedent(
        """
        import json
        import os
        import select
        import subprocess
        import sys
        import time

        sentinel_script = '''
        import signal
        import sys
        import time

        signal.signal(signal.SIGUSR1, lambda signum, frame: sys.exit(0))
        print("READY", flush=True)
        time.sleep(60)
        '''
        sentinel = subprocess.Popen(
            [sys.executable, "-c", sentinel_script],
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ["PROBE_OPERATOR_HOME"],
                "PYTHONUTF8": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = sentinel.stdout.readline().strip()
        if ready != "READY":
            raise RuntimeError(f"sentinel failed readiness: {ready!r}")
        print(sentinel.pid, flush=True)

        stop_requested = False
        while sentinel.poll() is None:
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if readable and sys.stdin.readline().strip() == "stop":
                stop_requested = True
                sentinel.terminate()
        returncode = sentinel.wait(timeout=10)
        print(json.dumps({
            "returncode": returncode,
            "stop_requested": stop_requested,
        }), flush=True)
        """
    )
    owner_env = _unmarked_env(operator_home=operator_home)
    owner_env["PROBE_OPERATOR_HOME"] = str(operator_home)
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script],
        cwd=REPO_ROOT,
        env=owner_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert owner.stdout is not None
    assert owner.stdin is not None
    sentinel_pid = int(owner.stdout.readline().strip())

    child_script = textwrap.dedent(
        """
        import json
        import os

        from hermes_cli.gateway import _graceful_restart_via_sigusr1

        result = _graceful_restart_via_sigusr1(
            int(os.environ["TARGET_PID"]),
            drain_timeout=0.05,
        )
        print(json.dumps({"result": result}))
        """
    )
    child_env = (
        _child_env(operator_home=operator_home, isolation_home=isolation_home)
        if marked
        else _unmarked_env(operator_home=operator_home)
    )
    child_env["TARGET_PID"] = str(sentinel_pid)
    try:
        child = subprocess.run(
            [sys.executable, "-c", child_script],
            cwd=REPO_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        payload = json.loads(child.stdout.strip().splitlines()[-1])
        try:
            owner.wait(timeout=1)
            sentinel_survived_probe = False
        except subprocess.TimeoutExpired:
            sentinel_survived_probe = True
            owner.stdin.write("stop\n")
            owner.stdin.flush()
            owner.wait(timeout=10)
        owner_tail = owner.stdout.read().strip().splitlines()
        assert owner.returncode == 0, owner.stderr.read() if owner.stderr else ""
        owner_payload = json.loads(owner_tail[-1])
        return payload, bool(
            sentinel_survived_probe and owner_payload["stop_requested"]
        )
    finally:
        if owner.poll() is None:
            owner.stdin.write("stop\n")
            owner.stdin.flush()
            owner.wait(timeout=10)


def test_marked_cli_cannot_resolve_profile_from_inherited_home(tmp_path):
    operator_home = tmp_path / "operator"
    isolation_home = tmp_path / "isolation"
    operator_profile = operator_home / ".hermes" / "profiles" / "victim"
    isolated_profile = isolation_home / "profiles" / "victim"
    operator_profile.mkdir(parents=True)
    isolated_profile.mkdir(parents=True)

    script = textwrap.dedent(
        """
        import json
        import os
        import sys

        sys.argv = ["hermes", "-p", "victim", "--version"]
        import hermes_cli.main  # noqa: F401

        print(json.dumps({"hermes_home": os.environ.get("HERMES_HOME")}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=_child_env(
            operator_home=operator_home,
            isolation_home=isolation_home,
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(payload["hermes_home"]).resolve() == isolated_profile.resolve()
    assert Path(payload["hermes_home"]).resolve() != operator_profile.resolve()


def test_marked_gateway_replace_cannot_signal_unmarked_sibling(tmp_path):
    """A child CLI may not SIGTERM a live PID outside its isolation tree."""
    operator_home = tmp_path / "operator"
    isolation_home = tmp_path / "isolation"
    operator_home.mkdir()
    isolation_home.mkdir()

    # Deliberately scrub the marker from the sentinel.  It represents a host
    # gateway from the marked CLI child's point of view, while remaining a
    # disposable Python child owned by this test.
    sentinel_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(operator_home),
        "PYTHONUTF8": "1",
    }
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=sentinel_env,
    )
    try:
        script = textwrap.dedent(
            """
            import asyncio
            import json
            import os

            import gateway.run as gateway_run
            import gateway.status as status
            import hermes_logging
            import tools.skills_sync
            from gateway.config import GatewayConfig

            target_pid = int(os.environ["HERMES_TEST_SENTINEL_PID"])
            status.get_running_pid = lambda: target_pid
            status.get_process_start_time = lambda pid: None
            status._snapshot_gateway_children = lambda pid: []
            status.reap_gateway_children = lambda *args, **kwargs: 0
            status.release_all_scoped_locks = lambda **kwargs: 0
            status.remove_pid_file = lambda: None
            tools.skills_sync.sync_skills = lambda quiet=True: None
            hermes_logging.setup_logging = lambda **kwargs: None

            class CleanRunner:
                def __init__(self, config):
                    self.config = config
                    self.should_exit_cleanly = True
                    self.should_exit_with_failure = False
                    self.exit_reason = None
                    self.exit_code = None
                    self.adapters = {}

                async def start(self):
                    return True

                async def stop(self):
                    return None

            gateway_run.GatewayRunner = CleanRunner
            result = asyncio.run(
                gateway_run.start_gateway(
                    config=GatewayConfig(), replace=True, verbosity=None
                )
            )
            print(json.dumps({"result": result}))
            """
        )
        child_env = _child_env(
            operator_home=operator_home,
            isolation_home=isolation_home,
        )
        child_env["HERMES_TEST_SENTINEL_PID"] = str(sentinel.pid)
        child = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert child.returncode == 0, child.stderr
        payload = json.loads(child.stdout.strip().splitlines()[-1])
        assert payload == {"result": False}
        assert sentinel.poll() is None, (
            "marked gateway child signalled a PID outside its disposable "
            "isolation tree"
        )
        assert not (operator_home / ".hermes" / ".gateway-takeover.json").exists()
        assert not (isolation_home / ".gateway-takeover.json").exists()
    finally:
        if sentinel.poll() is None:
            sentinel.terminate()
        sentinel.wait(timeout=10)


@pytest.mark.linux_only
def test_marked_self_restart_cannot_signal_unmarked_ancestor(tmp_path):
    operator_home = tmp_path / "operator"
    isolation_home = tmp_path / "isolation"
    operator_home.mkdir()
    isolation_home.mkdir()

    probe = _run_self_restart_probe(
        operator_home=operator_home,
        isolation_home=isolation_home,
        marked=True,
    )

    assert probe == {"helper_result": False, "received_sigusr1": False}


@pytest.mark.linux_only
def test_unmarked_self_restart_preserves_production_signal(tmp_path):
    operator_home = tmp_path / "operator"
    isolation_home = tmp_path / "unused-isolation"
    operator_home.mkdir()
    isolation_home.mkdir()

    probe = _run_self_restart_probe(
        operator_home=operator_home,
        isolation_home=isolation_home,
        marked=False,
    )

    assert probe == {"helper_result": True, "received_sigusr1": True}


@pytest.mark.linux_only
def test_marked_graceful_restart_cannot_signal_unmarked_sentinel(tmp_path):
    operator_home = tmp_path / "operator"
    isolation_home = tmp_path / "isolation"
    operator_home.mkdir()
    isolation_home.mkdir()

    payload, sentinel_survived = _run_graceful_restart_probe(
        operator_home=operator_home,
        isolation_home=isolation_home,
        marked=True,
    )

    assert payload == {"result": False}
    assert sentinel_survived is True


@pytest.mark.linux_only
def test_unmarked_graceful_restart_preserves_production_signal(tmp_path):
    operator_home = tmp_path / "operator"
    isolation_home = tmp_path / "unused-isolation"
    operator_home.mkdir()
    isolation_home.mkdir()

    payload, sentinel_survived = _run_graceful_restart_probe(
        operator_home=operator_home,
        isolation_home=isolation_home,
        marked=False,
    )

    assert payload == {"result": True}
    assert sentinel_survived is False
