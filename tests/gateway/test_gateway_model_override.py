"""Gateway process-local model route regression coverage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.model_override import GatewayModelOverride


def test_override_rejects_secret_values_and_credential_urls():
    with pytest.raises(ValueError, match="environment variable name"):
        GatewayModelOverride.build(key_env="sk-secret-value")
    with pytest.raises(ValueError, match="without credentials"):
        GatewayModelOverride.build(base_url="https://user:pass@example.test/v1")


def test_override_resolves_key_by_name_without_retaining_value(monkeypatch):
    import gateway.run as gateway_run

    override = GatewayModelOverride.build(
        model="hermes",
        provider="custom",
        base_url="https://gateway.example.test/v1",
        api_mode="chat_completions",
        key_env="DIRECTOR_LITELLM_KEY",
    )
    assert override is not None
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        lambda _provider: (_ for _ in ()).throw(RuntimeError("not registered")),
    )
    monkeypatch.setattr(
        "agent.secret_scope.get_secret",
        lambda name: "runtime-only-secret" if name == "DIRECTOR_LITELLM_KEY" else None,
    )
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._startup_model_override = override

    model, runtime = runner._apply_gateway_startup_model_override(
        "shared-model", {"provider": "shared", "api_key": "shared-key"}
    )

    assert model == "hermes"
    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == "https://gateway.example.test/v1"
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["api_key"] == "runtime-only-secret"
    assert "runtime-only-secret" not in repr(override)


def test_override_fails_closed_when_named_key_is_missing(monkeypatch):
    import gateway.run as gateway_run

    override = GatewayModelOverride.build(key_env="DIRECTOR_LITELLM_KEY")
    assert override is not None
    monkeypatch.setattr("agent.secret_scope.get_secret", lambda _name: None)
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._startup_model_override = override

    with pytest.raises(RuntimeError, match="DIRECTOR_LITELLM_KEY"):
        runner._apply_gateway_startup_model_override("shared", {})


def test_channel_route_wins_over_gateway_startup_route(monkeypatch):
    import gateway.run as gateway_run
    from gateway.session_state import SessionState

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = SimpleNamespace()
    runner._startup_model_override = GatewayModelOverride.build(
        model="gateway-model", provider="gateway-provider"
    )
    runner._sessions = {}
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _cfg: "shared-model")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"provider": "shared-provider", "api_key": "shared-key"},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {"provider": provider, "api_key": f"{provider}-key"},
    )
    monkeypatch.setattr(
        gateway_run,
        "_get_channel_override",
        lambda *_args, **_kwargs: SimpleNamespace(
            model="channel-model", provider="channel-provider"
        ),
    )
    runner._session_key_for_source = lambda _source: "telegram:1"
    runner._rehydrate_session_model_override = lambda _key: None
    runner._peek_session_state = lambda key: runner._sessions.get(key)
    runner._session_state = lambda key: runner._sessions.setdefault(key, SessionState())
    source = SimpleNamespace(
        platform="telegram", chat_id="1", thread_id=None, parent_chat_id=None
    )

    model, runtime = runner._resolve_session_agent_runtime(source=source)

    assert model == "channel-model"
    assert runtime["provider"] == "channel-provider"


def test_override_is_rejected_for_profile_multiplexer(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run.GatewayRunner, "_warn_if_docker_media_delivery_is_risky", lambda _self: None)
    with pytest.raises(ValueError, match="multiplex_profiles"):
        gateway_run.GatewayRunner(
            SimpleNamespace(multiplex_profiles=True),
            model_override=GatewayModelOverride.build(model="hermes"),
        )
