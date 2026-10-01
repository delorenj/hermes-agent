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


# ── route-limit 403s fall back (fork) ────────────────────────────────────
#
# Live (2026-09-30): every kimi-* route on the gateway answered 403 "You've
# reached your weekly (7-day) usage limit" (upstream Kimi Coding cap relayed by
# NewAPI), and a token missing a route's scope answers 403 "This token has no
# access to model X". The main agent fell back on those 403s; aux did not:
# _is_payment_error does not match the wording and a 403 is not an auth error,
# so title/vision/compression raised with no fallback at all. On a gateway each
# route is its own upstream account, so a 403 is MODEL scope: try the siblings.

KIMI_WEEKLY_403 = _status_error(
    403,
    "{'error': {'message': \"You've reached your weekly (7-day) usage limit. "
    "Your quota will reset when the current 7-day window ends. To continue now, "
    "purchase extra usage or upgrade your plan\", 'type': 'access_terminated_error', "
    "'param': '', 'code': None}}",
    openai.PermissionDeniedError,
)
SCOPE_MISS_403 = _status_error(
    403,
    "{'error': {'message': 'This token has no access to model gw/personal/glm-5.3', "
    "'type': 'new_api_error'}}",
    openai.PermissionDeniedError,
)
# Billing-worded ("weekly usage limit", "upgrade for higher limits" are
# _is_payment_error keywords): on a gateway it is still ONE route's limit.
BILLING_WORDED_403 = _status_error(
    403,
    "You've reached your weekly usage limit. Upgrade for higher limits.",
    openai.PermissionDeniedError,
)
BAD_CREDENTIALS_403 = _status_error(
    403, "unauthenticated:bad-credentials", openai.PermissionDeniedError,
)
PLAIN_403 = _status_error(403, "Forbidden", openai.PermissionDeniedError)

ROUTE_LIMIT_403S = [KIMI_WEEKLY_403, SCOPE_MISS_403, BILLING_WORDED_403]
ROUTE_LIMIT_IDS = ["kimi-weekly-cap", "token-scope-miss", "billing-worded"]


def test_route_limit_classifier(gateway_env):
    _write_config(gateway_env.home, discovery=False)
    from agent.auxiliary_client import _is_route_limit_error
    from agent.backend_identity import FailureScope, classify_failure_scope

    for make in ROUTE_LIMIT_403S + [PLAIN_403]:
        err = make(GATEWAY)
        # The gateway slug, the runtime ``custom`` label and an ``auto`` route
        # at the gateway URL are all the named gateway route.
        assert _is_route_limit_error(err, provider="automaticai", base_url=GATEWAY)
        assert _is_route_limit_error(err, provider="auto", base_url=GATEWAY)
        assert _is_route_limit_error(err, provider="custom:automaticai")

    direct = "https://api.direct-vendor.test/v1"
    # Off the gateway only limit wording counts, and payment keeps its own.
    assert _is_route_limit_error(KIMI_WEEKLY_403(direct), provider="kimi-coding", base_url=direct)
    assert not _is_route_limit_error(PLAIN_403(direct), provider="kimi-coding", base_url=direct)
    assert not _is_route_limit_error(BILLING_WORDED_403(direct), provider="opencode-go", base_url=direct)
    # Never a credential failure, never another status.
    assert not _is_route_limit_error(
        BAD_CREDENTIALS_403(GATEWAY), provider="automaticai", base_url=GATEWAY)
    assert not _is_route_limit_error(PAYMENT_402(GATEWAY), provider="automaticai", base_url=GATEWAY)
    assert not _is_route_limit_error(RATE_429(GATEWAY), provider="automaticai", base_url=GATEWAY)
    assert classify_failure_scope("route limit") is FailureScope.MODEL


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("error", ROUTE_LIMIT_403S, ids=ROUTE_LIMIT_IDS)
@pytest.mark.parametrize("runtime_custom", [False, True], ids=["config-main", "runtime-custom-main"])
def test_route_limit_403_on_main_route_falls_back_to_gateway_sibling(
    gateway_env, discovery, error, runtime_custom,
):
    _write_config(gateway_env.home, discovery=discovery)
    aux = gateway_env.aux
    gateway_env.rec.errors = {MAIN: error}
    if runtime_custom:
        _runtime_custom_main(aux)
    try:
        resp = aux.call_llm(task="title_generation", messages=_msgs())
    finally:
        aux.clear_runtime_main()

    assert resp.choices[0].message.content == f"ok:{FB1}"
    assert gateway_env.rec.calls == [(GATEWAY, MAIN), (GATEWAY, FB1)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}
    # One route's cap never benches the whole gateway provider.
    assert aux._aux_unhealthy_until == {}


def test_route_limited_fallback_walks_to_the_next_declared_route(gateway_env):
    """A fallback route that is capped too hands off to the next one."""
    _write_config(gateway_env.home, discovery=False)
    gateway_env.rec.errors = {MAIN: KIMI_WEEKLY_403, FB1: KIMI_WEEKLY_403}

    resp = gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert resp.choices[0].message.content == f"ok:{FB2}"
    assert gateway_env.rec.calls == [(GATEWAY, MAIN), (GATEWAY, FB1), (GATEWAY, FB2)]
    assert gateway_env.spies == {}


def test_every_route_limited_raises_after_one_pass_on_the_gateway(gateway_env):
    _write_config(gateway_env.home, discovery=False)
    gateway_env.rec.errors = {MAIN: KIMI_WEEKLY_403, FB1: SCOPE_MISS_403, FB2: KIMI_WEEKLY_403}

    with pytest.raises(openai.PermissionDeniedError):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert gateway_env.rec.calls == [(GATEWAY, MAIN), (GATEWAY, FB1), (GATEWAY, FB2)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}


def test_fallback_walk_never_continues_past_a_connection_error(gateway_env):
    """The walk is for capacity failures only: a dead endpoint still raises."""
    _write_config(gateway_env.home, discovery=False)
    gateway_env.rec.errors = {MAIN: KIMI_WEEKLY_403}
    gateway_env.rec.failing = {FB1}

    with pytest.raises(openai.APIConnectionError):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert gateway_env.rec.calls == [(GATEWAY, MAIN), (GATEWAY, FB1)]


def test_bad_credentials_403_keeps_credential_handling(gateway_env):
    """A 403 that says the KEY is bad is auth: the explicit pinned task does
    not fall back (auth is not a capacity error), exactly as before."""
    _write_pinned_task_config(gateway_env.home, task="title_generation")
    gateway_env.rec.errors = {FLASH: BAD_CREDENTIALS_403}

    with pytest.raises(openai.PermissionDeniedError):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert gateway_env.rec.calls == [(GATEWAY, FLASH)]


@pytest.mark.parametrize("task", ["title_generation", "vision", "compression"])
@pytest.mark.parametrize("error", ROUTE_LIMIT_403S, ids=ROUTE_LIMIT_IDS)
@pytest.mark.parametrize("runtime_custom", [False, True], ids=["config-main", "runtime-custom-main"])
def test_pinned_task_route_limit_reaches_main_model(gateway_env, task, error, runtime_custom):
    """An aux task pinned to a capped gateway model (explicit provider) falls
    back to the main model on the gateway via the safety net."""
    _write_pinned_task_config(gateway_env.home, task=task)
    aux = gateway_env.aux
    gateway_env.rec.errors = {FLASH: error}
    if runtime_custom:
        _runtime_custom_main(aux)
    try:
        resp = aux.call_llm(task=task, messages=_msgs())
    finally:
        aux.clear_runtime_main()

    assert resp.choices[0].message.content == f"ok:{MAIN}"
    assert gateway_env.rec.calls == [(GATEWAY, FLASH), (GATEWAY, MAIN)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}


def test_pinned_task_route_limit_walks_its_fallback_chain(gateway_env):
    """A pinned task's own fallback_chain is walked past a capped entry."""
    _write_pinned_task_config(gateway_env.home, task="vision")
    cfg = yaml.safe_load((gateway_env.home / "config.yaml").read_text())
    cfg["auxiliary"]["vision"]["fallback_chain"] = [
        {"provider": "automaticai", "model": FB2},
        {"provider": "automaticai", "model": FB1},
    ]
    (gateway_env.home / "config.yaml").write_text(yaml.safe_dump(cfg))
    gateway_env.rec.errors = {FLASH: KIMI_WEEKLY_403, FB2: KIMI_WEEKLY_403}

    resp = gateway_env.aux.call_llm(task="vision", messages=_msgs())

    assert resp.choices[0].message.content == f"ok:{FB1}"
    assert gateway_env.rec.calls == [(GATEWAY, FLASH), (GATEWAY, FB2), (GATEWAY, FB1)]


@pytest.mark.parametrize("error", ROUTE_LIMIT_403S, ids=ROUTE_LIMIT_IDS)
def test_async_route_limit_403_falls_back_and_walks(async_gateway_env, error):
    import asyncio

    env = async_gateway_env
    _write_config(env.home, discovery=False)
    env.rec.errors = {MAIN: error, FB1: error}

    resp = asyncio.run(env.aux.async_call_llm(task="title_generation", messages=_msgs()))

    assert resp.choices[0].message.content == f"ok:{FB2}"
    assert _gateway_calls(env.rec) == [MAIN, FB1, FB2]
    assert set(env.rec.clients) == {GATEWAY}
    assert env.spies == {}
    assert env.aux._aux_unhealthy_until == {}


def test_async_pinned_vision_route_limit_reaches_main_model(async_gateway_env):
    import asyncio

    env = async_gateway_env
    _write_pinned_task_config(env.home, task="vision")
    env.rec.errors = {FLASH: KIMI_WEEKLY_403}
    _runtime_custom_main(env.aux)
    try:
        resp = asyncio.run(env.aux.async_call_llm(task="vision", messages=_msgs()))
    finally:
        env.aux.clear_runtime_main()

    assert resp.choices[0].message.content == f"ok:{MAIN}"
    assert _gateway_calls(env.rec) == [FLASH, MAIN]


# ── explicit aux task falls through to fallback_providers (fork) ──────────
#
# Live (2026-10-01): title pinned to ``automaticai/personal/kimi-2.8`` with the
# main model ALSO kimi-2.8, both behind Kimi Coding's weekly cap. The pinned
# call 403'd, the task had no fallback_chain, and the main-model safety net
# skipped the main model (it IS the failed deployment), so the call raised
# "route limit on automaticai and all fallbacks exhausted (fallback_chain +
# main agent model)" while every other fallback_providers route on the gateway
# was healthy: _select_fallback never consulted fallback_providers for an
# explicit provider. Order now: task fallback_chain -> main agent model ->
# main fallback_providers, walked past every capped deployment.

KIMI = FB2  # the capped gateway route


def _write_capped_main_pinned_config(home, *, task, discovery=False, task_chain=None):
    task_cfg = {"provider": "automaticai", "model": KIMI}
    if task_chain is not None:
        task_cfg["fallback_chain"] = task_chain
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "automaticai", "default": KIMI, "api_mode": "chat_completions"},
        "providers": {
            "automaticai": {
                "name": "AutomaticAI",
                "api": GATEWAY,
                "key_env": "GW_TEST_KEY",
                "default_model": KIMI,
                "api_mode": "chat_completions",
                "models": [KIMI, FB1, MAIN],
            },
        },
        # The fleet chain shape: the capped model is listed too.
        "fallback_providers": [
            {"provider": "automaticai", "model": FB1},
            {"provider": "automaticai", "model": MAIN},
            {"provider": "automaticai", "model": KIMI},
        ],
        "auxiliary": {"transient_retries": 0, "discovery": discovery, task: task_cfg},
    }))


def _runtime_custom_kimi_main(aux):
    aux.set_runtime_main("custom", KIMI, base_url=GATEWAY, api_key="k",
                         api_mode="chat_completions")


# 429 quota exhaustion as the gateway is about to relay it (OpenRouter
# workspace budget, OpenAI-style insufficient_quota). The canonical OpenAI
# wording carries "billing", which _is_payment_error owns.
QUOTA_429_OPENAI = _status_error(
    429,
    "{'error': {'message': 'You exceeded your current quota, please check your "
    "plan and billing details.', 'type': 'insufficient_quota', 'param': None, "
    "'code': 'insufficient_quota'}}",
    openai.RateLimitError,
)
QUOTA_429_BUDGET = _status_error(
    429,
    "{'error': {'message': 'Workspace daily budget of $12.00 exceeded', "
    "'type': 'insufficient_quota', 'param': '', 'code': 'insufficient_quota'}}",
    openai.RateLimitError,
)
QUOTA_429S = [QUOTA_429_OPENAI, QUOTA_429_BUDGET]
QUOTA_429_IDS = ["429-insufficient-quota-billing-worded", "429-insufficient-quota-budget"]

CAPPED_ERRORS = [KIMI_WEEKLY_403, BILLING_WORDED_403] + QUOTA_429S
CAPPED_IDS = ["kimi-weekly-cap", "billing-worded-403"] + QUOTA_429_IDS


@pytest.mark.parametrize("task", ["title_generation", "vision"])
@pytest.mark.parametrize("error", CAPPED_ERRORS, ids=CAPPED_IDS)
@pytest.mark.parametrize("runtime_custom", [False, True], ids=["config-main", "runtime-custom-main"])
def test_pinned_task_on_capped_main_model_falls_through_to_fallback_providers(
    gateway_env, task, error, runtime_custom,
):
    _write_capped_main_pinned_config(gateway_env.home, task=task)
    aux = gateway_env.aux
    gateway_env.rec.errors = {KIMI: error}
    if runtime_custom:
        _runtime_custom_kimi_main(aux)
    try:
        resp = aux.call_llm(task=task, messages=_msgs())
    finally:
        aux.clear_runtime_main()

    assert resp.choices[0].message.content == f"ok:{FB1}"
    # The capped deployment is sent once: never retried, never re-sent as the
    # main model or as the chain's own kimi entry.
    assert gateway_env.rec.calls == [(GATEWAY, KIMI), (GATEWAY, FB1)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}
    assert aux._aux_unhealthy_until == {}


def test_pinned_task_fall_through_walks_past_a_capped_fallback_provider(gateway_env):
    _write_capped_main_pinned_config(gateway_env.home, task="title_generation")
    gateway_env.rec.errors = {KIMI: KIMI_WEEKLY_403, FB1: QUOTA_429_OPENAI}

    resp = gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert resp.choices[0].message.content == f"ok:{MAIN}"
    assert gateway_env.rec.calls == [(GATEWAY, KIMI), (GATEWAY, FB1), (GATEWAY, MAIN)]
    assert gateway_env.spies == {}


def test_pinned_task_fall_through_runs_after_its_own_fallback_chain(gateway_env):
    """The task's fallback_chain still comes first; fallback_providers only
    once it is exhausted."""
    _write_capped_main_pinned_config(
        gateway_env.home, task="title_generation",
        task_chain=[{"provider": "automaticai", "model": MAIN}],
    )
    gateway_env.rec.errors = {KIMI: KIMI_WEEKLY_403, MAIN: KIMI_WEEKLY_403}

    resp = gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert resp.choices[0].message.content == f"ok:{FB1}"
    assert gateway_env.rec.calls == [(GATEWAY, KIMI), (GATEWAY, MAIN), (GATEWAY, FB1)]


def test_pinned_task_tries_main_model_before_fallback_providers(gateway_env):
    """Pinned to a non-main model: the main model (safety net) is tried first,
    and when it is capped too the walk reaches fallback_providers."""
    _write_pinned_task_config(gateway_env.home, task="title_generation")
    gateway_env.rec.errors = {FLASH: KIMI_WEEKLY_403, MAIN: QUOTA_429_BUDGET}

    resp = gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert resp.choices[0].message.content == f"ok:{FB1}"
    assert gateway_env.rec.calls == [(GATEWAY, FLASH), (GATEWAY, MAIN), (GATEWAY, FB1)]
    assert gateway_env.spies == {}


def test_pinned_task_every_route_capped_raises_after_one_pass(gateway_env):
    _write_capped_main_pinned_config(gateway_env.home, task="title_generation")
    gateway_env.rec.errors = {KIMI: KIMI_WEEKLY_403, FB1: QUOTA_429_OPENAI, MAIN: KIMI_WEEKLY_403}

    with pytest.raises(openai.PermissionDeniedError):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert gateway_env.rec.calls == [(GATEWAY, KIMI), (GATEWAY, FB1), (GATEWAY, MAIN)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}


def test_pinned_task_auth_error_still_never_falls_back(gateway_env):
    """Auth is not a capacity error: the explicit-provider gate holds."""
    _write_capped_main_pinned_config(gateway_env.home, task="title_generation")
    gateway_env.rec.errors = {KIMI: BAD_CREDENTIALS_403}

    with pytest.raises(openai.PermissionDeniedError):
        gateway_env.aux.call_llm(task="title_generation", messages=_msgs())

    assert gateway_env.rec.calls == [(GATEWAY, KIMI)]


def test_main_chain_credential_failure_on_a_foreign_provider_keeps_main_entries(gateway_env):
    """A 402 on an explicit non-main provider kills THAT credential, not the
    main provider's: its fallback_providers entries stay candidates. A 402 on
    the gateway itself still skips every gateway entry."""
    _write_config(gateway_env.home, discovery=False)
    aux = gateway_env.aux
    _client, model, label = aux._try_main_fallback_chain(
        "title_generation", "openrouter", reason="payment error",
        failed_model="google/gemini-3-flash", failed_base_url="https://openrouter.ai/api/v1",
    )
    assert (model, label) == (FB1, "automaticai")
    for failed in ("automaticai", "custom:automaticai"):
        assert aux._try_main_fallback_chain(
            "title_generation", failed, reason="payment error",
            failed_model=MAIN, failed_base_url=GATEWAY,
        ) == (None, None, "")
    _runtime_custom_main(aux)
    try:
        assert aux._try_main_fallback_chain(
            "title_generation", "custom", reason="payment error",
            failed_model=MAIN, failed_base_url=GATEWAY,
        ) == (None, None, "")
    finally:
        aux.clear_runtime_main()


@pytest.mark.parametrize("error", CAPPED_ERRORS, ids=CAPPED_IDS)
def test_async_pinned_task_on_capped_main_model_falls_through(async_gateway_env, error):
    import asyncio

    env = async_gateway_env
    _write_capped_main_pinned_config(env.home, task="vision")
    env.rec.errors = {KIMI: error, FB1: error}
    _runtime_custom_kimi_main(env.aux)
    try:
        resp = asyncio.run(env.aux.async_call_llm(task="vision", messages=_msgs()))
    finally:
        env.aux.clear_runtime_main()

    assert resp.choices[0].message.content == f"ok:{MAIN}"
    assert _gateway_calls(env.rec) == [KIMI, FB1, MAIN]
    assert set(env.rec.clients) == {GATEWAY}
    assert env.spies == {}
    assert env.aux._aux_unhealthy_until == {}


# ── 429 insufficient_quota on a gateway is ONE route's limit (fork) ───────
#
# The gateway relays an upstream account's quota/budget exhaustion (OpenRouter
# workspace "daily budget of $12.00 exceeded") as 429 insufficient_quota. Only
# that route's upstream account is out: the gateway key and its sibling routes
# are fine. The billing-worded form used to be a payment error (credential
# scope: siblings skipped, gateway benched, walk stopped).


def test_quota_429_classifier(gateway_env):
    _write_config(gateway_env.home, discovery=False)
    from agent.auxiliary_client import _is_route_limit_error

    for make in QUOTA_429S:
        err = make(GATEWAY)
        assert _is_route_limit_error(err, provider="automaticai", base_url=GATEWAY)
        assert _is_route_limit_error(err, provider="auto", base_url=GATEWAY)
        assert _is_route_limit_error(err, provider="custom:automaticai")
    direct = "https://api.openai.test/v1"
    # Off the gateway an insufficient_quota 429 is the account's: unchanged.
    assert not _is_route_limit_error(QUOTA_429_OPENAI(direct), provider="openai", base_url=direct)
    # A plain gateway rate limit stays a rate limit (same-target retry kept).
    assert not _is_route_limit_error(RATE_429(GATEWAY), provider="automaticai", base_url=GATEWAY)


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("error", QUOTA_429S, ids=QUOTA_429_IDS)
@pytest.mark.parametrize("runtime_custom", [False, True], ids=["config-main", "runtime-custom-main"])
def test_quota_429_on_main_route_walks_gateway_siblings(gateway_env, discovery, error, runtime_custom):
    _write_config(gateway_env.home, discovery=discovery)
    aux = gateway_env.aux
    gateway_env.rec.errors = {MAIN: error, FB1: error}
    if runtime_custom:
        _runtime_custom_main(aux)
    try:
        resp = aux.call_llm(task="title_generation", messages=_msgs())
    finally:
        aux.clear_runtime_main()

    assert resp.choices[0].message.content == f"ok:{FB2}"
    assert gateway_env.rec.calls == [(GATEWAY, MAIN), (GATEWAY, FB1), (GATEWAY, FB2)]
    assert set(gateway_env.rec.clients) == {GATEWAY}
    assert gateway_env.spies == {}
    assert aux._aux_unhealthy_until == {}


@pytest.mark.parametrize("error", QUOTA_429S, ids=QUOTA_429_IDS)
def test_async_quota_429_on_main_route_walks_gateway_siblings(async_gateway_env, error):
    import asyncio

    env = async_gateway_env
    _write_config(env.home, discovery=False)
    env.rec.errors = {MAIN: error, FB1: error}

    resp = asyncio.run(env.aux.async_call_llm(task="title_generation", messages=_msgs()))

    assert resp.choices[0].message.content == f"ok:{FB2}"
    assert _gateway_calls(env.rec) == [MAIN, FB1, FB2]
    assert env.spies == {}
    assert env.aux._aux_unhealthy_until == {}
