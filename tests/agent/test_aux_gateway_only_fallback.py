"""Aux fallback stays on declared routes (fork, D3).

Repro (fleet, 2026-09-30): main provider ``automaticai`` (a named custom
provider fronting an inference gateway), ``fallback_providers`` = two more
gateway models under the SAME provider.  When an aux call on the main route
failed (connection / payment / capacity):

(a) ``_try_main_fallback_chain`` skipped every fallback entry because it
    shares the main provider, so no gateway fallback model was ever tried;
(b) ``_try_payment_fallback`` then walked the discovery chain and landed on a
    DIRECT provider via whatever key the process env exported (Gemini, z.ai,
    Kimi, OpenRouter).

(a) now skips only the exact (provider, model) that failed.  (b) is the new
``auxiliary.discovery`` switch: false = never walk discovery.
"""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import openai
import pytest
import yaml

GATEWAY = "https://api.example-gateway.test/v1"
MAIN = "gw/personal/glm-5.3"
FB1 = "gw/personal/sol-6.1"
FB2 = "gw/personal/kimi-2.8"
DIRECT_KEYS = ("GEMINI_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY", "GLM_API_KEY")


def _write_config(home, *, discovery):
    aux = {"transient_retries": 0}
    if discovery is not None:
        aux["discovery"] = discovery
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "automaticai", "default": MAIN, "api_mode": "chat_completions"},
        "providers": {
            "automaticai": {
                "name": "AutomaticAI",
                "api": GATEWAY,
                "key_env": "GW_TEST_KEY",
                "default_model": MAIN,
                "api_mode": "chat_completions",
                "models": [MAIN, FB1, FB2],
            },
        },
        "fallback_providers": [
            {"provider": "automaticai", "model": FB1},
            {"provider": "automaticai", "model": FB2},
        ],
        "auxiliary": aux,
    }))


class _Recorder:
    def __init__(self):
        self.failing = set()
        self.errors = {}    # model -> callable(base_url) returning the error to raise
        self.clients = []   # base_url of every OpenAI-wire client built
        self.calls = []     # (base_url, model) of every request sent

    def build(self, *, api_key, base_url, **_kwargs):
        rec = self

        class _Completions:
            def create(self_inner, **kw):
                model = kw.get("model")
                rec.calls.append((str(base_url).rstrip("/"), model))
                if model in rec.errors:
                    raise rec.errors[model](base_url)
                if model in rec.failing:
                    raise openai.APIConnectionError(
                        message="Connection error.",
                        request=httpx.Request("POST", f"{base_url}/chat/completions"),
                    )
                return SimpleNamespace(
                    model=model,
                    usage=None,
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            role="assistant", content=f"ok:{model}", tool_calls=None,
                        ),
                    )],
                )

        self.clients.append(str(base_url).rstrip("/"))
        return SimpleNamespace(
            api_key=api_key,
            base_url=base_url,
            chat=SimpleNamespace(completions=_Completions()),
            close=lambda: None,
        )


@pytest.fixture
def gateway_env(tmp_path, monkeypatch):
    import agent.auxiliary_client as aux

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GW_TEST_KEY", "gw-test-key-not-a-secret")
    for key in DIRECT_KEYS:
        monkeypatch.setenv(key, f"direct-{key.lower()}-not-a-secret")
    for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    def _reset():
        with aux._client_cache_lock:
            aux._client_cache.clear()
        aux._aux_unhealthy_until.clear()
        getattr(aux, "_DISCOVERY_OFF_LOGGED", set()).clear()

    _reset()
    rec = _Recorder()
    spies = {}
    originals = {
        name: getattr(aux, name)
        for name in ("_try_openrouter", "_try_nous", "_try_custom_endpoint", "_resolve_api_key_provider")
    }

    def _spy(name):
        def _wrapped(*a, **kw):
            spies[name] = spies.get(name, 0) + 1
            return originals[name](*a, **kw)
        return _wrapped

    with patch.object(aux, "_create_openai_client", side_effect=rec.build):
        for name in originals:
            monkeypatch.setattr(aux, name, _spy(name))
        yield SimpleNamespace(home=home, rec=rec, spies=spies, aux=aux)
    _reset()


def _msgs():
    return [{"role": "user", "content": "name this session"}]


# ── (a) same provider, different model is a valid aux fallback ──────────


def test_main_chain_skips_only_the_failed_deployment(gateway_env):
    _write_config(gateway_env.home, discovery=False)
    aux = gateway_env.aux
    client, model, label = aux._try_main_fallback_chain(
        "title_generation", "auto", reason="connection error",
        failed_model=MAIN, failed_base_url=GATEWAY,
    )
    assert client is not None and model == FB1 and label == "automaticai"
    assert str(client.base_url).rstrip("/") == GATEWAY

    client, model, _ = aux._try_main_fallback_chain(
        "title_generation", "automaticai", reason="connection error", failed_model=FB1,
    )
    assert model == FB2  # only FB1 (the failed deployment) is skipped


def test_connection_error_on_main_route_falls_back_to_next_gateway_model(gateway_env):
    _write_config(gateway_env.home, discovery=False)
    gateway_env.rec.failing = {MAIN}

    resp = gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert resp.choices[0].message.content == f"ok:{FB1}"
    assert gateway_env.rec.calls == [(GATEWAY, MAIN), (GATEWAY, FB1)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}


# ── (b) auxiliary.discovery: false never reaches a direct provider ──────


def test_discovery_off_all_routes_failing_never_builds_a_direct_client(gateway_env):
    _write_config(gateway_env.home, discovery=False)
    gateway_env.rec.failing = {MAIN, FB1, FB2}

    with pytest.raises(openai.APIConnectionError):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert {url for url, _ in gateway_env.rec.calls} == {GATEWAY}
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}


def test_discovery_off_payment_fallback_returns_none(gateway_env):
    _write_config(gateway_env.home, discovery=False)
    assert gateway_env.aux._try_payment_fallback("auto", "title_generation") == (None, None, "")
    assert gateway_env.spies == {}


def test_discovery_off_unavailable_gateway_returns_none(gateway_env):
    """Main + every fallback_providers entry unavailable (the gateway provider
    is quarantined after a 402) -> None, never a direct-provider client."""
    _write_config(gateway_env.home, discovery=False)
    gateway_env.aux._mark_provider_unhealthy("automaticai")

    client, model = gateway_env.aux._resolve_auto(task="title_generation")

    assert client is None and model is None
    assert gateway_env.rec.clients == []
    assert gateway_env.spies == {}


def test_discovery_default_true_keeps_upstream_discovery(gateway_env):
    """Default (key absent) and explicit true still walk the discovery chain."""
    for discovery in (None, True):
        _write_config(gateway_env.home, discovery=discovery)
        gateway_env.aux._mark_provider_unhealthy("automaticai")
        gateway_env.rec.clients.clear()
        gateway_env.spies.clear()
        with gateway_env.aux._client_cache_lock:
            gateway_env.aux._client_cache.clear()

        client, _model = gateway_env.aux._resolve_auto(task="title_generation")

        assert client is not None
        assert GATEWAY not in str(client.base_url)
        assert gateway_env.spies.get("_try_openrouter", 0) >= 1

        _client, _m, label = gateway_env.aux._try_payment_fallback(
            "automaticai", "title_generation", reason="connection error")
        assert label  # a discovery-chain provider served


def test_runtime_custom_failure_skips_same_gateway_model_only(gateway_env):
    """Live shape: the failed call ran on runtime provider ``custom`` at the
    gateway URL. A fallback entry naming the same model on ``automaticai`` (the
    configured slug for that URL) is the same deployment; other models are not."""
    _write_config(gateway_env.home, discovery=False)
    aux = gateway_env.aux
    cfg = yaml.safe_load((gateway_env.home / "config.yaml").read_text())
    cfg["fallback_providers"].insert(0, {"provider": "automaticai", "model": MAIN})
    (gateway_env.home / "config.yaml").write_text(yaml.safe_dump(cfg))
    aux.set_runtime_main("custom", MAIN, base_url=GATEWAY, api_key="k",
                         api_mode="chat_completions")
    try:
        client, model, label = aux._try_main_fallback_chain(
            "title_generation", "auto", reason="connection error",
            failed_model=MAIN, failed_base_url=GATEWAY,
        )
    finally:
        aux.clear_runtime_main()
    assert model == FB1 and label == "automaticai"


# ── failure scope decides what the main fallback chain skips (D3 fix) ────
#
# 688bacfac4 passed failed_model for EVERY failure and hard-coded
# FailureScope.MODEL, so an account-wide 402 tried a same-provider sibling
# and, when that sibling failed too, re-raised without ever reaching
# discovery. The scope now comes from classify_failure_scope(reason).


def _status_error(status, message, cls=openai.APIStatusError):
    def _make(base_url):
        request = httpx.Request("POST", f"{base_url}/chat/completions")
        return cls(
            f"Error code: {status} - {message}",
            response=httpx.Response(status, request=request),
            body=None,
        )
    return _make


PAYMENT_402 = _status_error(402, "insufficient account quota")
RATE_429 = _status_error(429, "rate limit exceeded, try again later", openai.RateLimitError)
NO_CHANNEL_503 = _status_error(
    503,
    "{'error': {'message': 'No available channel for model gw/personal/glm-5.3 "
    "under group default (distributor)', 'type': 'new_api_error'}}",
    openai.InternalServerError,
)
GENERIC_503 = _status_error(503, "Service Unavailable", openai.InternalServerError)


def _gateway_calls(rec):
    return [model for url, model in rec.calls if url == GATEWAY]


@pytest.mark.parametrize("runtime_custom", [False, True], ids=["config-main", "runtime-custom-main"])
def test_payment_error_with_discovery_on_skips_gateway_siblings_and_reaches_discovery(
    gateway_env, runtime_custom,
):
    """402 is credential scope: every route on the gateway key is dead, so no
    sibling is tried and discovery serves (adc4cd4059 behaviour). The live
    shape (runtime provider ``custom`` at the gateway URL) maps back to the
    configured ``automaticai`` slug."""
    _write_config(gateway_env.home, discovery=True)
    aux = gateway_env.aux
    gateway_env.rec.errors = {MAIN: PAYMENT_402, FB1: PAYMENT_402, FB2: PAYMENT_402}
    if runtime_custom:
        aux.set_runtime_main("custom", MAIN, base_url=GATEWAY, api_key="k",
                             api_mode="chat_completions")
    try:
        resp = aux.call_llm(task="title_generation", messages=_msgs())
    finally:
        aux.clear_runtime_main()

    assert _gateway_calls(gateway_env.rec) == [MAIN]
    served_url, served_model = gateway_env.rec.calls[-1]
    assert served_url != GATEWAY
    assert resp.choices[0].message.content == f"ok:{served_model}"
    assert gateway_env.spies.get("_try_openrouter", 0) >= 1


def test_main_chain_payment_reason_skips_the_whole_credential(gateway_env):
    _write_config(gateway_env.home, discovery=True)
    aux = gateway_env.aux
    assert aux._try_main_fallback_chain(
        "title_generation", "auto", reason="payment error",
        failed_model=MAIN, failed_base_url=GATEWAY,
    ) == (None, None, "")
    assert aux._try_main_fallback_chain(
        "title_generation", "auto", reason="auth error",
        failed_model=MAIN, failed_base_url=GATEWAY,
    ) == (None, None, "")
    _client, model, _label = aux._try_main_fallback_chain(
        "title_generation", "auto", reason="rate limit",
        failed_model=MAIN, failed_base_url=GATEWAY,
    )
    assert model == FB1


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize(
    "error", [RATE_429, NO_CHANNEL_503], ids=["429-rate-limit", "503-no-available-channel"],
)
def test_model_scoped_failure_tries_the_gateway_sibling(gateway_env, discovery, error):
    _write_config(gateway_env.home, discovery=discovery)
    gateway_env.rec.errors = {MAIN: error}

    resp = gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert resp.choices[0].message.content == f"ok:{FB1}"
    assert gateway_env.rec.calls == [(GATEWAY, MAIN), (GATEWAY, FB1)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}


def test_generic_503_keeps_transient_only_behaviour(gateway_env):
    """Only a 503 that names the model's route as unavailable falls back."""
    _write_config(gateway_env.home, discovery=True)
    gateway_env.rec.errors = {MAIN: GENERIC_503}

    with pytest.raises(openai.InternalServerError):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert gateway_env.rec.calls == [(GATEWAY, MAIN)]
    assert gateway_env.spies == {}


@pytest.mark.parametrize(
    "error,expected",
    [
        (PAYMENT_402, openai.APIStatusError),
        (RATE_429, openai.RateLimitError),
        (NO_CHANNEL_503, openai.InternalServerError),
    ],
    ids=["402", "429", "503-no-channel"],
)
def test_discovery_off_never_builds_a_direct_client_for_any_scope(gateway_env, error, expected):
    _write_config(gateway_env.home, discovery=False)
    gateway_env.rec.errors = {MAIN: error, FB1: error, FB2: error}

    with pytest.raises(expected):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert {url for url, _ in gateway_env.rec.calls} == {GATEWAY}
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}
    if error is PAYMENT_402:
        assert _gateway_calls(gateway_env.rec) == [MAIN]  # credential scope: no sibling


def test_model_unavailable_classifier_is_503_and_marker_gated():
    from agent.auxiliary_client import _is_model_unavailable_error
    from agent.backend_identity import FailureScope, classify_failure_scope

    assert _is_model_unavailable_error(NO_CHANNEL_503(GATEWAY))
    assert _is_model_unavailable_error(
        _status_error(503, "分组 default 下模型 x 无可用渠道（distributor）",
                      openai.InternalServerError)(GATEWAY))
    assert not _is_model_unavailable_error(GENERIC_503(GATEWAY))
    assert not _is_model_unavailable_error(
        _status_error(500, "No available channel for model x")(GATEWAY))
    assert classify_failure_scope("model unavailable") is FailureScope.MODEL


class _AsyncWrap:
    """Async face over a recorder client (the async path converts fallback
    clients with ``_to_async_client``)."""

    def __init__(self, sync):
        self._sync = sync
        self.base_url = sync.base_url
        self.api_key = sync.api_key
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        return self._sync.chat.completions.create(**kw)

    async def close(self):
        return None


@pytest.fixture
def async_gateway_env(gateway_env, monkeypatch):
    monkeypatch.setattr(
        gateway_env.aux, "_to_async_client",
        lambda client, model, is_vision=False: (_AsyncWrap(client), model),
    )
    return gateway_env


def test_async_payment_error_reaches_discovery_without_trying_siblings(async_gateway_env):
    import asyncio

    env = async_gateway_env
    _write_config(env.home, discovery=True)
    env.rec.errors = {MAIN: PAYMENT_402, FB1: PAYMENT_402, FB2: PAYMENT_402}

    resp = asyncio.run(env.aux.async_call_llm(task="title_generation", messages=_msgs()))

    assert set(_gateway_calls(env.rec)) == {MAIN}
    served_url, served_model = env.rec.calls[-1]
    assert served_url != GATEWAY
    assert resp.choices[0].message.content == f"ok:{served_model}"


@pytest.mark.parametrize(
    "error", [RATE_429, NO_CHANNEL_503], ids=["429-rate-limit", "503-no-available-channel"],
)
def test_async_model_scoped_failure_tries_the_gateway_sibling(async_gateway_env, error):
    import asyncio

    env = async_gateway_env
    _write_config(env.home, discovery=False)
    env.rec.errors = {MAIN: error}

    resp = asyncio.run(env.aux.async_call_llm(task="title_generation", messages=_msgs()))

    assert resp.choices[0].message.content == f"ok:{FB1}"
    assert _gateway_calls(env.rec)[-1] == FB1
    assert set(env.rec.clients) == {GATEWAY}
    assert env.spies == {}


# ── explicit aux task: main-model safety net behind a runtime ``custom`` ──
#
# Live shape (2026-09-30): vision pinned to a gateway model, main provider
# ``automaticai`` reporting itself as ``custom`` once a turn ran. The safety
# net (_try_main_agent_model_fallback) asked resolve_provider_client for the
# bare ``custom`` provider, which resolves no endpoint, so a failed pinned
# model got no fallback at all. The runtime ``custom`` route now maps back to
# the configured slug and the net lands on the main model on the gateway.

FLASH = "gw/personal/glm-5.3-flash"


def _write_pinned_task_config(home, *, task, discovery=False):
    _write_config(home, discovery=discovery)
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    cfg["auxiliary"][task] = {"provider": "automaticai", "model": FLASH}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))


def _runtime_custom_main(aux, base_url=GATEWAY):
    aux.set_runtime_main("custom", MAIN, base_url=base_url, api_key="k",
                         api_mode="chat_completions")


@pytest.mark.parametrize("task", ["title_generation", "vision"])
@pytest.mark.parametrize(
    "error", [None, RATE_429, NO_CHANNEL_503], ids=["connection", "429", "503-no-channel"],
)
def test_pinned_task_safety_net_reaches_main_model_behind_runtime_custom(
    gateway_env, task, error,
):
    _write_pinned_task_config(gateway_env.home, task=task)
    aux = gateway_env.aux
    if error is None:
        gateway_env.rec.failing = {FLASH}
    else:
        gateway_env.rec.errors = {FLASH: error}
    _runtime_custom_main(aux)
    try:
        resp = aux.call_llm(task=task, messages=_msgs())
    finally:
        aux.clear_runtime_main()

    assert resp.choices[0].message.content == f"ok:{MAIN}"
    # 429 may be retried on the pinned model first; the net then serves MAIN.
    assert gateway_env.rec.calls[-1] == (GATEWAY, MAIN)
    assert {call for call in gateway_env.rec.calls[:-1]} == {(GATEWAY, FLASH)}
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}


def test_safety_net_maps_runtime_custom_to_the_configured_slug(gateway_env):
    _write_pinned_task_config(gateway_env.home, task="title_generation")
    aux = gateway_env.aux
    _runtime_custom_main(aux)
    try:
        client, model, label = aux._try_main_agent_model_fallback(
            "automaticai", "title_generation", reason="connection error",
            failed_model=FLASH,
        )
    finally:
        aux.clear_runtime_main()
    assert model == MAIN and label == "main-agent(automaticai)"
    assert str(client.base_url).rstrip("/") == GATEWAY


@pytest.mark.parametrize("failed_provider", ["automaticai", "custom"])
def test_safety_net_credential_failure_on_the_gateway_skips_main(gateway_env, failed_provider):
    """402/401 on the gateway key: the main model shares that key, so the net
    is skipped whether the failed route reported the slug or runtime custom."""
    _write_pinned_task_config(gateway_env.home, task="title_generation")
    aux = gateway_env.aux
    _runtime_custom_main(aux)
    try:
        result = aux._try_main_agent_model_fallback(
            failed_provider, "title_generation", reason="payment error",
            failed_model=None, failed_base_url=GATEWAY,
        )
    finally:
        aux.clear_runtime_main()
    assert result == (None, None, "")
    assert gateway_env.rec.clients == []


def test_safety_net_skips_main_when_main_is_the_failed_deployment(gateway_env):
    """A runtime-custom failure of the main model itself has nothing to fall to."""
    _write_pinned_task_config(gateway_env.home, task="title_generation")
    aux = gateway_env.aux
    _runtime_custom_main(aux)
    try:
        result = aux._try_main_agent_model_fallback(
            "custom", "title_generation", reason="connection error",
            failed_model=MAIN, failed_base_url=GATEWAY,
        )
    finally:
        aux.clear_runtime_main()
    assert result == (None, None, "")


def test_safety_net_never_maps_a_foreign_custom_endpoint(gateway_env):
    """Runtime ``custom`` on some other URL is not the configured gateway
    entry: no mapping, and with discovery off no client off the gateway."""
    _write_pinned_task_config(gateway_env.home, task="title_generation")
    aux = gateway_env.aux
    _runtime_custom_main(aux, base_url="https://elsewhere.example.test/v1")
    try:
        client, _model, label = aux._try_main_agent_model_fallback(
            "automaticai", "title_generation", reason="connection error",
            failed_model=FLASH,
        )
    finally:
        aux.clear_runtime_main()
    assert label != "main-agent(automaticai)"
    assert all(url == GATEWAY for url in gateway_env.rec.clients)
    if client is not None:
        assert str(client.base_url).rstrip("/") != "https://openrouter.ai/api/v1"
