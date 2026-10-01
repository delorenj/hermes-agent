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
        self.clients = []   # base_url of every OpenAI-wire client built
        self.calls = []     # (base_url, model) of every request sent

    def build(self, *, api_key, base_url, **_kwargs):
        rec = self

        class _Completions:
            def create(self_inner, **kw):
                model = kw.get("model")
                rec.calls.append((str(base_url).rstrip("/"), model))
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
