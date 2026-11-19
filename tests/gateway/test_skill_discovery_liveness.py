"""PJAN-74: real gateway skill reload yields to heartbeat and watchdog probes."""

import asyncio
from contextlib import suppress
from itertools import pairwise
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from tests.waiting import async_wait_until


async def _exercise_gateway_discovery(tmp_path, monkeypatch, cadence):
    from agent import skill_commands
    from gateway import shutdown_watchdog as guards
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import skills_tool

    home = tmp_path / "owner"
    skill = home / "skills" / "liveness-demo"
    skill.mkdir(parents=True)
    (home / "config.yaml").write_text("skills:\n  project_discovery: false\n")
    (skill / "SKILL.md").write_text("---\nname: liveness-demo\ndescription: Liveness fixture.\n---\nInstructions.\n")
    runner = GatewayRunner(GatewayConfig())
    runner.adapters = {}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="fixture", user_id="fixture", chat_type="dm")
    event = MessageEvent(text="/reload-skills", source=source, message_id="fixture")
    skill_commands._skill_commands_by_key.clear()
    skills_tool.clear_skills_cache()
    original_scandir = os.scandir
    entered, release = threading.Event(), threading.Event()
    scanner_threads = []
    writes = []
    original_write = guards.write_loop_heartbeat
    loop_thread = threading.get_ident()
    pid = os.getpid()

    def slow_scandir(path):
        if Path(path) == home / "skills":
            scanner_threads.append(threading.get_ident())
            if not entered.is_set():
                entered.set()
                if not release.wait(timeout=4 * cadence + 20):
                    raise AssertionError("owner loop could not release actual skill scan")
        return original_scandir(path)

    def heartbeat(**kwargs):
        path = original_write(**kwargs)
        writes.append(json.loads(path.read_text())["monotonic"])
        return path

    monkeypatch.setattr(os, "scandir", slow_scandir)
    monkeypatch.setattr(guards, "write_loop_heartbeat", heartbeat)
    monkeypatch.setattr(guards, "get_shutdown_watchdog_dump_path",
                        lambda home_override=None: home / "logs" / "gateway-shutdown-watchdog.log")
    assert guards.get_shutdown_watchdog_dump_path() == guards.get_shutdown_watchdog_dump_path(home)
    # If the watchdog fires, kill ONLY this test's child process. Never stamp live gateway state.
    monkeypatch.setattr(guards, "_hard_exit", lambda code: os._exit(code))
    monkeypatch.setattr(guards, "_mark_exited_quietly", lambda *args: None)
    probe_delays = []
    original_call_soon = asyncio.get_running_loop().call_soon_threadsafe
    loop = asyncio.get_running_loop()

    def observed_call_soon(callback, *args, **kwargs):
        if callback.__name__ == "set" and isinstance(getattr(callback, "__self__", None), threading.Event):
            requested = time.monotonic()

            def probe():
                probe_delays.append(time.monotonic() - requested)
                callback(*args)

            return original_call_soon(probe, **kwargs)
        return original_call_soon(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", observed_call_soon)
    # The 30s case uses the production heartbeat/probe cadence and unchanged 10s/3-strike
    # threshold; the accelerated case provides a fast scheduling regression too.
    watchdog = guards.start_loop_liveness_watchdog(asyncio.get_running_loop(), probe_interval=cadence)
    assert watchdog is not None
    heartbeat_task = asyncio.create_task(guards.loop_heartbeat_forever(interval_s=cadence, home=home))
    token = set_hermes_home_override(str(home))
    started = time.monotonic()
    reload_task = asyncio.create_task(runner._hm_dispatch_canonical_command(event, source, "fixture", "reload-skills"))
    try:
        await async_wait_until(entered.is_set, timeout=20, what="real skill directory scan")
        await async_wait_until(lambda: len(writes) >= 4, timeout=4 * cadence + 10, what="heartbeats during blocked discovery")
        assert not reload_task.done()
        scan_blocked_s = time.monotonic() - started
        release.set()
        handled, reply = await asyncio.wait_for(reload_task, 15)
        assert handled and reply is not None and "liveness-demo" in reply
        assert scanner_threads and all(t != loop_thread for t in scanner_threads)
        gaps = [b - a for a, b in pairwise(writes)]
        assert max(gaps) - cadence < guards.DEFAULT_LOOP_WATCHDOG_TIMEOUT_S
        assert scan_blocked_s >= 3 * cadence
        assert probe_delays and max(probe_delays) < guards.DEFAULT_LOOP_WATCHDOG_TIMEOUT_S
        assert watchdog.is_alive() and os.getpid() == pid
        assert not guards.get_shutdown_watchdog_dump_path(home).exists()
        print(json.dumps({"scan_blocked_s": scan_blocked_s, "heartbeat_interval_s": cadence,
                          "production_heartbeat_interval_s": guards.DEFAULT_HEARTBEAT_INTERVAL_S,
                          "heartbeat_writes": len(writes), "max_heartbeat_gap_s": max(gaps),
                          "watchdog_probes": len(probe_delays), "max_watchdog_probe_latency_s": max(probe_delays),
                          "watchdog_timeout_s": guards.DEFAULT_LOOP_WATCHDOG_TIMEOUT_S,
                          "watchdog_max_strikes": guards.DEFAULT_LOOP_WATCHDOG_MAX_STRIKES,
                          "pid_unchanged": os.getpid() == pid, "restart": False}))
    finally:
        release.set()
        reset_hermes_home_override(token)
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        if not reload_task.done():
            await asyncio.wait_for(reload_task, 20)
        watchdog.stop()
        await asyncio.to_thread(watchdog.join, 2)
        monkeypatch.setattr(loop, "call_soon_threadsafe", original_call_soon)
        executor = getattr(runner, "_executor", None)
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, wait=True)


@pytest.mark.parametrize("cadence", [1, 30])
def test_actual_skill_discovery_keeps_gateway_heartbeat_live(tmp_path, cadence):
    """A real watchdog may hard-exit; isolate it from pytest and every live profile."""
    child_home = tmp_path / "child-home"
    test_root = tmp_path / "pytest"
    test_root.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio, sys; from pathlib import Path; "
         "from _pytest.monkeypatch import MonkeyPatch; "
         "from tests.gateway.test_skill_discovery_liveness import _exercise_gateway_discovery; "
         "m=MonkeyPatch(); "
         "asyncio.run(_exercise_gateway_discovery(Path(sys.argv[1]), m, int(sys.argv[2]))); "
         "m.undo()", str(test_root), str(cadence)],
        env={**os.environ, "HERMES_HOME": str(child_home)},
        capture_output=True, text=True, timeout=4 * cadence + 40, check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["heartbeat_writes"] >= 4 and receipt["watchdog_probes"] >= 1
    assert receipt["max_watchdog_probe_latency_s"] < receipt["watchdog_timeout_s"]
    print(json.dumps(receipt))
