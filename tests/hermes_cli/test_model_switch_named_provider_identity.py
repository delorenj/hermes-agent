"""A ``providers.<slug>`` entry and its derived ``custom:<Name>`` view are ONE
provider when ``/model`` resolves a typed name.

Repro (fleet, 2026-09-30): config declares ``providers.automaticai`` (name
``AutomaticAI``, ``api`` + ``key_env``).  ``load_picker_context()`` passes that
dict as ``user_providers`` AND its compatibility projection from
``get_compatible_custom_providers()`` as ``custom_providers`` (slug
``custom:AutomaticAI``).  Before the first turn the CLI's current provider is
``automaticai`` and step d.5 keeps it.  After the first turn the runtime
provider is ``custom`` (named custom providers resolve to ``custom`` at
runtime), it matches neither candidate, and the same entry counted twice made
``/model automaticai/personal/glm-5.3`` fail with "declared by multiple
configured providers (automaticai, custom:AutomaticAI)".
"""

from unittest.mock import patch

import pytest

from hermes_cli.config import get_compatible_custom_providers
from hermes_cli.model_switch import _configured_provider_matches, switch_model

GATEWAY = "https://api.example-gateway.test/v1"
MODEL = "gw/personal/glm-5.3"

_ACCEPTED = {"accepted": True, "persist": True, "recognized": True, "message": None}


def _config(**extra_providers):
    providers = {
        "automaticai": {
            "name": "AutomaticAI",
            "api": GATEWAY,
            "key_env": "GW_TEST_KEY",
            "default_model": MODEL,
            "api_mode": "chat_completions",
            "extra_body": {"reasoning_effort": "high"},
            "models": [MODEL, "gw/personal/sol-6.1"],
        },
    }
    providers.update(extra_providers)
    return {"providers": providers}


def _picker_inputs(cfg):
    """What ``load_picker_context()`` hands ``switch_model``."""
    return cfg["providers"], get_compatible_custom_providers(cfg)


def _run_switch(*, raw_input, current_provider, current_base_url, cfg):
    user_providers, custom_providers = _picker_inputs(cfg)
    with patch("hermes_cli.model_switch.resolve_alias", return_value=None), \
         patch("hermes_cli.model_switch.list_provider_models", return_value=[]), \
         patch("hermes_cli.model_switch.normalize_model_for_provider", side_effect=lambda model, provider: model), \
         patch("hermes_cli.models.validate_requested_model", return_value=_ACCEPTED), \
         patch("hermes_cli.models.detect_provider_for_model", return_value=None), \
         patch("hermes_cli.model_switch.get_model_info", return_value=None), \
         patch("hermes_cli.model_switch.get_model_capabilities", return_value=None), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={"api_key": "***", "base_url": current_base_url, "api_mode": ""},
         ):
        return switch_model(
            raw_input=raw_input,
            current_provider=current_provider,
            current_model=MODEL,
            current_base_url=current_base_url,
            current_api_key="***",
            user_providers=user_providers,
            custom_providers=custom_providers,
        )


@pytest.fixture(autouse=True)
def _gateway_key(monkeypatch):
    monkeypatch.setenv("GW_TEST_KEY", "gw-test-key-not-a-secret")


def test_derived_custom_entry_is_not_a_second_provider():
    user_providers, custom_providers = _picker_inputs(_config())
    # The compat view really does carry a derived custom:AutomaticAI entry.
    assert any(e.get("provider_key") == "automaticai" for e in custom_providers)
    assert _configured_provider_matches(MODEL, user_providers, custom_providers) == {
        "automaticai": MODEL,
    }


def test_switch_after_first_turn_runtime_custom_resolves_named_provider():
    """Runtime provider ``custom`` on the gateway URL == providers.automaticai."""
    result = _run_switch(
        raw_input=MODEL,
        current_provider="custom",
        current_base_url=GATEWAY,
        cfg=_config(),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "automaticai"
    assert result.new_model == MODEL
    assert result.base_url.rstrip("/") == GATEWAY


def test_switch_before_first_turn_still_keeps_named_provider():
    result = _run_switch(
        raw_input=MODEL,
        current_provider="automaticai",
        current_base_url=GATEWAY,
        cfg=_config(),
    )
    assert result.success is True, result.error_message
    assert result.target_provider == "automaticai"


def test_same_endpoint_and_key_env_under_two_slugs_is_one_provider():
    """Identity dedupe: same api + key_env declared twice is one provider."""
    cfg = _config(
        gateway_alias={
            "name": "Gateway Alias",
            "base_url": GATEWAY + "/",
            "key_env": "GW_TEST_KEY",
            "models": [MODEL],
        },
    )
    user_providers, custom_providers = _picker_inputs(cfg)
    matches = _configured_provider_matches(MODEL, user_providers, custom_providers)
    assert list(matches) == ["automaticai"]


def test_genuinely_ambiguous_model_still_errors():
    """Two DIFFERENT providers declaring the model is still ambiguous."""
    cfg = _config(
        other={
            "name": "Other Relay",
            "base_url": "https://relay.other.test/v1",
            "key_env": "OTHER_TEST_KEY",
            "models": [MODEL],
        },
    )
    result = _run_switch(
        raw_input=MODEL,
        current_provider="custom",
        current_base_url="http://127.0.0.1:9/v1",
        cfg=cfg,
    )
    assert result.success is False
    assert "declared by multiple configured providers" in (result.error_message or "")
    assert "automaticai" in result.error_message and "other" in result.error_message
    assert "custom:" not in result.error_message
