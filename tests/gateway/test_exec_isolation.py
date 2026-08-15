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
