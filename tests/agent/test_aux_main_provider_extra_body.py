"""Aux requests on the main named custom provider carry its extra_body (fork, D4).

Repro (fleet, 2026-09-30): ``providers.automaticai.extra_body`` is
``{reasoning_effort: high}``.  The main agent sends it every turn, but aux
tasks resolved via ``auto`` -> main provider (title_generation, compression,
...) did not, so they ran at the gateway route's default effort (the ledger
showed ``effort_defaulted=true`` for title_generation).
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


def _write_config(home, *, aux=None, extra_body=None):
    provider = {
        "name": "AutomaticAI",
        "api": GATEWAY,
        "key_env": "GW_TEST_KEY",
        "default_model": MAIN,
        "api_mode": "chat_completions",
        "models": [MAIN, FB1, "gw/personal/glm-5.3-flash"],
    }
    if extra_body is not None:
        provider["extra_body"] = extra_body
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "automaticai", "default": MAIN, "api_mode": "chat_completions"},
        "providers": {"automaticai": provider},
        "fallback_providers": [{"provider": "automaticai", "model": FB1}],
        "auxiliary": {"transient_retries": 0, **(aux or {})},
    }))


@pytest.fixture
def env(tmp_path, monkeypatch):
    import agent.auxiliary_client as aux

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GW_TEST_KEY", "gw-test-key-not-a-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "direct-openrouter-not-a-secret")
    for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    requests = []
    failing = set()

    def _build(*, api_key, base_url, **_kw):
        def _create(**kw):
            requests.append({"base_url": str(base_url).rstrip("/"), **kw})
            if kw.get("model") in failing:
                raise openai.APIConnectionError(
                    message="Connection error.",
                    request=httpx.Request("POST", f"{base_url}/chat/completions"),
                )
            return SimpleNamespace(
                model=kw.get("model"), usage=None,
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(role="assistant", content="ok", tool_calls=None),
                )],
            )

        return SimpleNamespace(
            api_key=api_key, base_url=base_url, close=lambda: None,
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
        )

    def _reset():
        with aux._client_cache_lock:
            aux._client_cache.clear()
        aux._aux_unhealthy_until.clear()

    _reset()
    with patch.object(aux, "_create_openai_client", side_effect=_build):
        yield SimpleNamespace(home=home, aux=aux, requests=requests, failing=failing)
    _reset()


def _call(env, task="title_generation", **kw):
    return env.aux.call_llm(
        task=task, messages=[{"role": "user", "content": "hi"}], **kw,
    )


def test_auto_task_on_main_named_provider_carries_provider_extra_body(env):
    _write_config(env.home, extra_body={"reasoning_effort": "high"})
    _call(env)
    (req,) = env.requests
    assert req["base_url"] == GATEWAY
    assert req["model"] == MAIN
    assert req["extra_body"]["reasoning_effort"] == "high"


def test_runtime_custom_route_to_main_endpoint_carries_provider_extra_body(env):
    """After the first turn the live runtime provider is ``custom`` on the
    gateway URL — still the main named provider."""
    _write_config(env.home, extra_body={"reasoning_effort": "high"})
    _call(env, main_runtime={
        "provider": "custom", "model": MAIN, "base_url": GATEWAY,
        "api_key": "gw-test-key-not-a-secret", "api_mode": "chat_completions",
    })
    assert env.requests[-1]["extra_body"]["reasoning_effort"] == "high"


def test_pinned_task_on_main_provider_carries_provider_extra_body(env):
    _write_config(
        env.home,
        extra_body={"reasoning_effort": "high"},
        aux={"compression": {"provider": "automaticai", "model": FB1}},
    )
    _call(env, task="compression")
    (req,) = env.requests
    assert req["model"] == FB1
    assert req["extra_body"]["reasoning_effort"] == "high"


def test_task_extra_body_wins_over_provider_extra_body(env):
    _write_config(
        env.home,
        extra_body={"reasoning_effort": "high", "x_provider_tag": "p"},
        aux={"title_generation": {"extra_body": {"x_task_tag": "t"}}},
    )
    _call(env)
    body = env.requests[-1]["extra_body"]
    assert body == {"x_task_tag": "t"}


def test_task_reasoning_choice_drops_provider_reasoning_keys(env):
    _write_config(
        env.home,
        extra_body={"reasoning_effort": "high", "x_provider_tag": "p"},
        aux={"title_generation": {"reasoning_effort": "low"}},
    )
    _call(env)
    body = env.requests[-1]["extra_body"]
    assert "reasoning_effort" not in body
    assert body["x_provider_tag"] == "p"
    assert body["reasoning"]["effort"] == "low"


def test_gateway_fallback_route_carries_it_and_direct_provider_does_not(env):
    _write_config(env.home, extra_body={"reasoning_effort": "high"})
    env.failing.add(MAIN)
    _call(env)
    assert [(r["base_url"], r["model"]) for r in env.requests] == [(GATEWAY, MAIN), (GATEWAY, FB1)]
    assert env.requests[-1]["extra_body"]["reasoning_effort"] == "high"

    # A non-main destination never inherits the main provider's body.
    assert env.aux._with_main_provider_extra_body(
        {}, task="title_generation", provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    ) == {}


def test_no_provider_extra_body_leaves_request_unchanged(env):
    _write_config(env.home)
    _call(env)
    assert "extra_body" not in env.requests[-1] or not env.requests[-1]["extra_body"].get("reasoning_effort")


def test_live_runtime_override_custom_still_finds_main_provider(env):
    """Live CLI/gateway shape: set_runtime_main() records provider ``custom``
    (the runtime identity of a named custom provider), so the runtime-aware
    _read_main_provider() says ``custom``. The configured slug must still be
    found (first live run showed title_generation at effort_defaulted=true)."""
    _write_config(env.home, extra_body={"reasoning_effort": "high"})
    env.aux.set_runtime_main(
        "custom", MAIN, requested_provider="automaticai", base_url=GATEWAY,
        api_key="gw-test-key-not-a-secret", api_mode="chat_completions",
    )
    try:
        assert env.aux._read_main_provider() == "custom"
        _call(env)
    finally:
        env.aux.clear_runtime_main()
    req = env.requests[-1]
    assert req["base_url"] == GATEWAY
    assert req["extra_body"]["reasoning_effort"] == "high"
