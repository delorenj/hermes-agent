"""Concurrent multiplex boot and parallel shutdown.

A host serving dozens of profiles used to start them one after another (each Telegram bot waits ~10s for its
first long-poll) and stop them the same way (home-channel notices, then adapter teardown, one by one). These pin
the concurrent versions: the bound is honoured, ``profile_startup_concurrency: 1`` is today's serial boot, one
failing profile does not sink the others, every profile runs under its own scope, a token two profiles share is
still refused deterministically (the first profile in order keeps it), and teardown takes about as long as the
slowest adapter.
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, MultiplexConfigError
from hermes_constants import get_hermes_home_override, set_hermes_home_override, reset_hermes_home_override


def _homes(tmp_path, names):
    out = []
    for name in names:
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        out.append((name, home))
    return out


def _runner(concurrency=None):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    if concurrency is not None:
        runner.config.profile_startup_concurrency = concurrency
    runner._running = True
    runner._primary_profile_name = "default"
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._profile_failed_platforms = {}
    runner._failed_platforms = {}
    runner.pairing_store = MagicMock()
    runner.pairing_stores = {}
    return runner


async def _start_secondaries(runner, homes):
    """``_start_secondary_profile_adapters`` against a fixed profile list."""
    with patch("gateway.run._multiplex_profile_homes", return_value=[("default", Path("/d")), *homes]), \
         patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
         patch("hermes_cli.profiles.profiles_to_serve", return_value=[]), \
         patch.object(GatewayRunner, "_restore_secondary_completion_ledgers", lambda *a, **k: None), \
         patch.object(GatewayRunner, "_record_served_profiles", lambda *a, **k: None):
        return await runner._start_secondary_profile_adapters()


class _Gauge:
    def __init__(self):
        self.now = 0
        self.peak = 0
        self.order = []

    async def hold(self, name, seconds=0.03):
        self.order.append(name)
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(seconds)
        finally:
            self.now -= 1


# ── startup ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrency_bound_is_honoured_and_overlaps(tmp_path):
    runner = _runner(concurrency=3)
    gauge = _Gauge()

    async def start_one(name, home, claimed):
        await gauge.hold(name)
        return 1

    runner._start_one_profile_adapters = start_one
    homes = _homes(tmp_path, [f"p{i}" for i in range(10)])
    assert await _start_secondaries(runner, homes) == 10
    assert gauge.peak == 3
    assert gauge.order == [f"p{i}" for i in range(10)]  # launched in profile order


@pytest.mark.asyncio
async def test_concurrency_one_is_serial(tmp_path):
    runner = _runner(concurrency=1)
    gauge = _Gauge()

    async def start_one(name, home, claimed):
        await gauge.hold(name, 0.01)
        return 1

    runner._start_one_profile_adapters = start_one
    assert await _start_secondaries(runner, _homes(tmp_path, ["a", "b", "c", "d"])) == 4
    assert gauge.peak == 1
    assert gauge.order == ["a", "b", "c", "d"]


@pytest.mark.parametrize("raw,expected", [(None, 8), (0, 8), (-3, 8), ("x", 8), (1, 1), (16, 16)])
def test_concurrency_config_key(raw, expected):
    cfg = GatewayConfig.from_dict({} if raw is None else {"gateway": {"profile_startup_concurrency": raw}})
    assert cfg.profile_startup_concurrency == expected
    runner = _runner()
    runner.config = cfg
    assert runner._profile_startup_concurrency() == cfg.profile_startup_concurrency


def test_concurrency_key_loads_from_config_yaml():
    from gateway.config import load_gateway_config
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text("gateway:\n  profile_startup_concurrency: 5\n", encoding="utf-8")
    assert load_gateway_config().profile_startup_concurrency == 5


@pytest.mark.asyncio
async def test_one_failing_profile_does_not_sink_the_rest(tmp_path):
    runner = _runner(concurrency=4)
    gauge = _Gauge()

    async def start_one(name, home, claimed):
        await gauge.hold(name)
        if name == "bad":
            raise RuntimeError("boom")
        return 2

    runner._start_one_profile_adapters = start_one
    homes = _homes(tmp_path, ["a", "bad", "c", "d", "e"])
    assert await _start_secondaries(runner, homes) == 8
    assert set(runner._served_profile_signatures) == {"a", "c", "d", "e"}  # "bad" is retried by the watcher


@pytest.mark.asyncio
async def test_multiplex_config_error_propagates_without_cancelling_inflight(tmp_path):
    runner = _runner(concurrency=2)
    finished = []

    async def start_one(name, home, claimed):
        if name == "a":
            raise MultiplexConfigError("open policy")
        await asyncio.sleep(0.05)
        finished.append(name)
        return 1

    runner._start_one_profile_adapters = start_one
    with pytest.raises(MultiplexConfigError):
        await _start_secondaries(runner, _homes(tmp_path, ["a", "b", "c", "d"]))
    assert "b" in finished  # was in flight alongside "a": ran to completion


@pytest.mark.asyncio
async def test_each_profile_runs_under_its_own_context(tmp_path):
    runner = _runner(concurrency=6)
    seen = {}

    async def start_one(name, home, claimed):
        token = set_hermes_home_override(str(home))
        try:
            for _ in range(5):  # interleave with the other profiles' tasks
                await asyncio.sleep(0.005)
                assert get_hermes_home_override() == str(home)
            seen[name] = get_hermes_home_override()
        finally:
            reset_hermes_home_override(token)
        return 1

    runner._start_one_profile_adapters = start_one
    homes = _homes(tmp_path, [f"p{i}" for i in range(6)])
    before = get_hermes_home_override()
    assert await _start_secondaries(runner, homes) == 6
    assert seen == {name: str(home) for name, home in homes}
    assert get_hermes_home_override() == before  # nothing leaked into the booting task


class _TokenAdapter:
    def __init__(self, token):
        self.token = token
        self.config = PlatformConfig(enabled=True)
        self.platform = Platform.TELEGRAM
        self.disconnected = False

    def __getattr__(self, name):
        if name.startswith("set_"):
            return lambda *a, **k: None
        raise AttributeError(name)

    async def disconnect(self):
        self.disconnected = True


def _real_start_runner(monkeypatch, adapters, *, config_delay=None, connect_ok=True, connect_delay=0.05):
    """A runner whose REAL ``_start_one_profile_adapters`` builds the given telegram adapter per profile.

    Returns ``(runner, connected_tokens)``. ``config_delay`` slows a profile's config load, so the profile
    reaches its claim step late.
    """
    runner = _runner(concurrency=8)
    runner.session_store = None
    runner._busy_text_mode = "queue"
    connects = []
    current = {}

    async def load_config(name, home):
        await asyncio.sleep((config_delay or {}).get(name, 0))
        cfg = GatewayConfig(multiplex_profiles=True)
        cfg.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
        current[asyncio.current_task()] = adapters[name]
        return cfg

    async def connect(adapter, platform, **_kw):
        connects.append(adapter.token)
        await asyncio.sleep(connect_delay)
        return connect_ok

    monkeypatch.setattr(runner, "_load_secondary_profile_config", load_config)
    monkeypatch.setattr(runner, "_create_adapter", lambda p, c: current[asyncio.current_task()])
    monkeypatch.setattr(runner, "_connect_adapter_with_timeout", connect)
    monkeypatch.setattr(runner, "_make_adapter_auth_check", lambda p, **kw: None)
    monkeypatch.setattr(runner, "_schedule_secondary_profile_startup_reconnect", lambda *a, **k: None)
    monkeypatch.setattr("gateway.run._platform_has_bot_credential", lambda p, c: True)
    return runner, connects


@pytest.mark.asyncio
async def test_shared_token_is_refused_deterministically_under_concurrency(tmp_path, monkeypatch):
    adapters = {"a": _TokenAdapter("shared"), "b": _TokenAdapter("shared"), "c": _TokenAdapter("other")}
    # "a" is slow to load its config: without ordering, "b" would claim the token first and "a" would lose.
    runner, connects = _real_start_runner(monkeypatch, adapters, config_delay={"a": 0.1})
    claimed = {}
    results = dict(await runner._start_profiles_bounded(_homes(tmp_path, ["a", "b", "c"]), claimed))
    assert all(not isinstance(r, BaseException) for r in results.values())
    assert [results[n][0] for n in "abc"] == [1, 0, 1]
    assert runner._profile_adapters["a"][Platform.TELEGRAM] is adapters["a"]
    assert not runner._profile_adapters.get("b")
    assert sorted(connects) == ["other", "shared"]  # the shared token connected exactly once
    assert set(claimed.values()) == {"a", "c"}


@pytest.mark.asyncio
async def test_inflight_claim_blocks_duplicate_and_failed_connect_releases_it(tmp_path, monkeypatch):
    adapters = {"a": _TokenAdapter("t"), "b": _TokenAdapter("t")}
    runner, connects = _real_start_runner(monkeypatch, adapters, connect_ok=False)
    claimed = {}
    results = dict(await runner._start_profiles_bounded(_homes(tmp_path, ["a", "b"]), claimed))
    # "b" was refused while "a" was connecting (no second poller on one bot); "a" owns nothing afterwards.
    assert results["a"][0] == 0 and results["b"][0] == 0
    assert connects == ["t"]
    assert adapters["a"].disconnected is True
    assert claimed == {}


# ── shutdown ───────────────────────────────────────────────────────────────────────────────────────────────


class _SlowAdapter:
    def __init__(self, delay, hang=False, boom=False):
        self.delay, self.hang, self.boom = delay, hang, boom
        self.disconnected = False
        self.cancelled = False

    async def cancel_background_tasks(self):
        self.cancelled = True

    async def disconnect(self):
        if self.boom:
            raise RuntimeError("disconnect exploded")
        await asyncio.sleep(3600 if self.hang else self.delay)
        self.disconnected = True


def _teardown_runner(monkeypatch, timeout=5.0):
    runner = object.__new__(GatewayRunner)
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", str(timeout))
    return runner


@pytest.mark.asyncio
async def test_teardown_all_runs_concurrently_and_tears_down_everything(monkeypatch):
    runner = _teardown_runner(monkeypatch)
    adapters = [_SlowAdapter(0.1) for _ in range(30)]
    jobs = [(a, Platform.TELEGRAM, f"p{i}") for i, a in enumerate(adapters)]
    started = time.monotonic()
    await runner._bounded_adapter_teardown_all(jobs)
    elapsed = time.monotonic() - started
    assert all(a.cancelled and a.disconnected for a in adapters)
    assert elapsed < 1.0  # serial would be 3.0s: 30 / 16 permits ~ two waves of 0.1s


@pytest.mark.asyncio
async def test_teardown_all_bounds_a_hung_adapter_and_survives_a_raise(monkeypatch):
    runner = _teardown_runner(monkeypatch, timeout=0.2)
    hung, boom = _SlowAdapter(0, hang=True), _SlowAdapter(0, boom=True)
    fine = [_SlowAdapter(0.05) for _ in range(5)]
    jobs = [(hung, Platform.TELEGRAM, "hung"), (boom, Platform.TELEGRAM, "boom")] + [
        (a, Platform.TELEGRAM, f"ok{i}") for i, a in enumerate(fine)
    ]
    started = time.monotonic()
    await asyncio.wait_for(runner._bounded_adapter_teardown_all(jobs), timeout=5)
    elapsed = time.monotonic() - started
    assert all(a.disconnected for a in fine)
    assert 0.15 < elapsed < 0.6  # the hung adapter's own bound (0.2s), not bound x adapters


@pytest.mark.asyncio
async def test_teardown_all_honours_concurrency_cap(monkeypatch):
    runner = _teardown_runner(monkeypatch)
    gauge = _Gauge()

    class _Counted(_SlowAdapter):
        async def disconnect(self):
            await gauge.hold("x", 0.02)

    jobs = [(_Counted(0), Platform.TELEGRAM, None) for _ in range(12)]
    await runner._bounded_adapter_teardown_all(jobs, concurrency=4)
    assert gauge.peak == 4
    await runner._bounded_adapter_teardown_all([])  # empty is a no-op


@pytest.mark.asyncio
async def test_stop_path_tears_down_primary_and_every_profile_together(monkeypatch):
    runner = _teardown_runner(monkeypatch)
    primary = _SlowAdapter(0.1)
    secondary = {f"p{i}": {Platform.TELEGRAM: _SlowAdapter(0.1)} for i in range(20)}
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {name: dict(amap) for name, amap in secondary.items()}
    runner._restart_requested = False
    runner._agent_cache_lock = None
    runner._agent_cache = None
    runner._finalize_shutdown_agents = lambda agents: asyncio.sleep(0)
    ctx = SimpleNamespace(active_agents={}, elapsed=lambda: 0.0)
    started = time.monotonic()
    await runner._stop_finalize_agents_and_adapters(ctx)
    assert time.monotonic() - started < 1.0
    assert primary.disconnected
    assert all(a.disconnected for amap in secondary.values() for a in amap.values())
    assert runner._profile_adapters == {}


class _NoticeAdapter:
    def __init__(self, gauge, fail=False):
        self.gauge, self.fail = gauge, fail
        self.sent = []

    async def send(self, chat_id, content, **kw):
        await self.gauge.hold(chat_id, 0.03)
        self.sent.append(chat_id)
        if self.fail:
            raise RuntimeError("Chat not found")
        return SimpleNamespace(success=True)


@pytest.mark.asyncio
async def test_home_channel_notices_go_out_concurrently_one_per_chat(monkeypatch):
    runner = object.__new__(GatewayRunner)
    gauge = _Gauge()
    runner._restart_requested = False
    runner._restart_command_source = None
    runner._snapshot_running_agents = lambda: []
    runner._notice_allowed = lambda *a, **k: True
    runner._thread_metadata_for_target = lambda *a, **k: None
    runner._served_profile_homes = {}
    adapters = {}
    configs = []
    for i in range(12):
        name = f"p{i}"
        adapters[name] = {Platform.TELEGRAM: _NoticeAdapter(gauge, fail=(i == 3))}
        # p0 and p1 share one group chat: it is owed ONE notice
        chat = "-100shared" if i in (0, 1) else f"-100{i}"
        home = SimpleNamespace(chat_id=chat, thread_id=None)
        configs.append((name, Platform.TELEGRAM, SimpleNamespace(home_channel=home)))
    runner._profile_adapters = adapters
    runner.adapters = {}
    runner._served_home_channel_configs = lambda: iter(configs)

    await runner._notify_active_sessions_of_shutdown()

    assert gauge.peak > 4  # overlapped (a serial pass peaks at 1); wall time is import-noisy, so not asserted
    shared = [chat for name in ("p0", "p1") for chat in adapters[name][Platform.TELEGRAM].sent]
    assert shared == ["-100shared"]  # exactly one of the two profiles sent it
    assert adapters["p3"][Platform.TELEGRAM].sent == ["-1003"]  # a failing chat never stops the others
    assert all(adapters[f"p{i}"][Platform.TELEGRAM].sent for i in range(2, 12))
