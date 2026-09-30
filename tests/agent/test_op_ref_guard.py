"""Credential readers must never hand out an unresolved ``op://`` reference (fork fix).

When 1Password resolution was skipped or a raw .env reload clobbered the resolved value, the literal
``op://Vault/Item/field`` string became the ``Authorization: Bearer`` value (Kimi 401 "API Key appears
to be invalid", OpenRouter 401 "Missing Authentication header"). Each reader now returns empty for it
and logs one WARNING naming the variable -- never the value.
"""

from __future__ import annotations

import logging

import pytest

from agent import op_ref_guard

OP_REF = "op://DeLoSecrets/kimi-coding/credential"


@pytest.fixture(autouse=True)
def _fresh_warned():
    op_ref_guard._reset_warned_for_tests()
    yield
    op_ref_guard._reset_warned_for_tests()


def _op_warnings(caplog, name):
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and name in r.getMessage() and "op://" in r.getMessage()
    ]


def test_refuse_op_ref_warns_once_and_never_logs_the_value(caplog):
    with caplog.at_level(logging.WARNING):
        assert op_ref_guard.refuse_op_ref("KIMI_API_KEY", OP_REF) == ""
        assert op_ref_guard.refuse_op_ref("KIMI_API_KEY", OP_REF) == ""
    warnings = _op_warnings(caplog, "KIMI_API_KEY")
    assert len(warnings) == 1
    assert "DeLoSecrets" not in caplog.text


def test_refuse_op_ref_passes_real_values_through():
    assert op_ref_guard.refuse_op_ref("KIMI_API_KEY", "sk-real") == "sk-real"
    assert op_ref_guard.refuse_op_ref("KIMI_API_KEY", "") == ""
    assert op_ref_guard.refuse_op_ref("KIMI_API_KEY", None) is None


def test_credential_pool_get_env_prefer_dotenv_refuses_op_ref(monkeypatch, caplog):
    from agent import credential_pool

    monkeypatch.setattr(credential_pool, "load_env", lambda: {"KIMI_API_KEY": OP_REF})
    monkeypatch.setenv("KIMI_API_KEY", OP_REF)  # the clobbered case: env is op:// too
    with caplog.at_level(logging.WARNING):
        assert credential_pool.get_env_prefer_dotenv("KIMI_API_KEY") == ""
    assert _op_warnings(caplog, "KIMI_API_KEY")


def test_credential_pool_get_env_prefer_dotenv_still_prefers_resolved_value(monkeypatch):
    from agent import credential_pool

    monkeypatch.setattr(credential_pool, "load_env", lambda: {"KIMI_API_KEY": OP_REF})
    monkeypatch.setenv("KIMI_API_KEY", "sk-resolved")
    assert credential_pool.get_env_prefer_dotenv("KIMI_API_KEY") == "sk-resolved"


def test_runtime_provider_getenv_refuses_op_ref(monkeypatch, caplog):
    from hermes_cli import runtime_provider

    monkeypatch.setenv("OPENROUTER_API_KEY", OP_REF)
    with caplog.at_level(logging.WARNING):
        assert runtime_provider._getenv("OPENROUTER_API_KEY", "") == ""
    assert _op_warnings(caplog, "OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real")
    assert runtime_provider._getenv("OPENROUTER_API_KEY", "") == "sk-or-real"


def test_auxiliary_scoped_key_env_refuses_op_ref(monkeypatch, caplog):
    from agent import auxiliary_client

    monkeypatch.setenv("GLM_API_KEY", OP_REF)
    with caplog.at_level(logging.WARNING):
        assert auxiliary_client._scoped_key_env("GLM_API_KEY") == ""
    assert _op_warnings(caplog, "GLM_API_KEY")
    monkeypatch.setenv("GLM_API_KEY", "glm-real")
    assert auxiliary_client._scoped_key_env("GLM_API_KEY") == "glm-real"


def test_auxiliary_scoped_key_env_refuses_op_ref_inside_a_secret_scope(monkeypatch):
    from agent import auxiliary_client
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    token = set_secret_scope({"GLM_API_KEY": OP_REF})
    try:
        assert auxiliary_client._scoped_key_env("GLM_API_KEY") == ""
    finally:
        reset_secret_scope(token)


def test_config_get_env_value_prefer_dotenv_falls_through_to_resolved_value(monkeypatch):
    """A raw op:// line in .env yields to the value 1Password resolved into the environment."""
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_env", lambda: {"KIMI_API_KEY": OP_REF})
    monkeypatch.setenv("KIMI_API_KEY", "sk-resolved")
    assert config_mod.get_env_value_prefer_dotenv("KIMI_API_KEY") == "sk-resolved"


def test_config_get_env_value_prefer_dotenv_refuses_unresolved_op_ref(monkeypatch, caplog):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_env", lambda: {"KIMI_API_KEY": OP_REF})
    monkeypatch.setenv("KIMI_API_KEY", OP_REF)
    with caplog.at_level(logging.WARNING):
        assert not config_mod.get_env_value_prefer_dotenv("KIMI_API_KEY")
    assert _op_warnings(caplog, "KIMI_API_KEY")

    monkeypatch.delenv("KIMI_API_KEY")
    assert not config_mod.get_env_value_prefer_dotenv("KIMI_API_KEY")


def test_config_get_env_value_prefer_dotenv_keeps_plain_dotenv_precedence(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_env", lambda: {"KIMI_API_KEY": "sk-from-dotenv"})
    monkeypatch.setenv("KIMI_API_KEY", "sk-stale-shell")
    assert config_mod.get_env_value_prefer_dotenv("KIMI_API_KEY") == "sk-from-dotenv"
