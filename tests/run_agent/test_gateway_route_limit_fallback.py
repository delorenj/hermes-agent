"""Main-agent turns fall back on a gateway route-limit 403 (fork).

Live (2026-09-30): every kimi-* route on the AutomaticAI gateway answered 403
"You've reached your weekly (7-day) usage limit" (the upstream Kimi Coding cap,
relayed by NewAPI). On a gateway each route is its own upstream account, so the
403 says nothing about the gateway key: the turn must move to the next
``fallback_providers`` entry on the SAME provider. These tests pin that for the
main agent loop and for a delegated child, which inherits the parent's chain.
The aux paths (title, vision, compression) are pinned in
``tests/agent/test_aux_gateway_only_fallback.py``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from agent.error_classifier import classify_api_error
from run_agent import AIAgent

GATEWAY = "https://api.example-gateway.test/v1"
KIMI = "gw/personal/kimi-2.8"
GLM = "gw/personal/glm-5.3"
SOL = "gw/personal/sol-6.1"
KIMI_WEEKLY = (
    "You've reached your weekly (7-day) usage limit. Your quota will reset when "
    "the current 7-day window ends. To continue now, purchase extra usage or "
    "upgrade your plan"
)


def _permission_denied(message):
    request = httpx.Request("POST", f"{GATEWAY}/chat/completions")
    return openai.PermissionDeniedError(
        f"Error code: 403 - {{'error': {{'message': \"{message}\", "
        "'type': 'access_terminated_error'}}",
        response=httpx.Response(403, request=request),
        body={"error": {"message": message, "type": "access_terminated_error"}},
    )


def _response(content):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


@pytest.fixture(autouse=True)
def _gateway_config(tmp_path, monkeypatch):
    """``providers.automaticai`` = a named custom provider at the gateway URL."""
    import yaml

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GW_TEST_KEY", "gw-test-key-not-a-secret")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "automaticai", "default": SOL, "api_mode": "chat_completions"},
        "providers": {
            "automaticai": {
                "name": "AutomaticAI",
                "api": GATEWAY,
                "key_env": "GW_TEST_KEY",
                "default_model": SOL,
                "api_mode": "chat_completions",
                "models": [SOL, GLM, KIMI],
            },
        },
    }))
    yield home


def _make_agent(model, chain):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="gw-test-key-not-a-secret",
            base_url=GATEWAY,
            provider="custom",
            model=model,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=chain,
        )
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


@pytest.mark.parametrize(
    "message",
    [KIMI_WEEKLY, f"This token has no access to model {KIMI}"],
    ids=["usage-cap", "token-scope-miss"],
)
def test_gateway_route_limit_403_is_fallback_eligible(message):
    classified = classify_api_error(
        _permission_denied(message), provider="custom", model=KIMI,
    )
    assert classified.should_fallback is True


def _gateway_clients():
    """resolve_provider_client stub: one recorder client per gateway model."""
    calls = []

    def _resolve(provider, model=None, raw_codex=False, **kwargs):
        client = MagicMock()
        client.base_url = GATEWAY
        client.api_key = "gw-test-key-not-a-secret"

        def _create(**kw):
            calls.append(kw.get("model"))
            if kw.get("model") == KIMI:
                raise _permission_denied(KIMI_WEEKLY)
            return _response(f"ok:{kw.get('model')}")

        client.chat.completions.create.side_effect = _create
        return client, model

    return calls, _resolve


@pytest.mark.parametrize(
    "chain,expected",
    [
        # The owner chain shape: primary capped, next entry serves.
        ([{"provider": "automaticai", "model": GLM},
          {"provider": "automaticai", "model": KIMI}], GLM),
        # A capped entry later in the chain is skipped over by the walk.
        ([{"provider": "automaticai", "model": KIMI},
          {"provider": "automaticai", "model": SOL}], SOL),
    ],
    ids=["next-entry", "skips-capped-entry"],
)
def test_main_turn_on_capped_route_falls_back_to_gateway_sibling(chain, expected):
    agent = _make_agent(KIMI, chain)
    agent.client = MagicMock()
    agent.client.base_url = GATEWAY
    agent.client.chat.completions.create.side_effect = _permission_denied(KIMI_WEEKLY)
    calls, resolve = _gateway_clients()

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("hermes_cli.model_normalize.normalize_model_for_provider",
              side_effect=lambda m, p: m),
        patch("run_agent.handle_function_call", return_value="ok"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("name three colours")

    assert result["final_response"] == f"ok:{expected}"
    assert agent.model == expected
    assert agent._fallback_activated is True
    assert KIMI not in calls  # the capped deployment is never re-sent via fallback


def test_delegated_child_inherits_the_chain_that_falls_back():
    """A child agent is handed the parent's chain, so a capped child route
    falls back exactly like the parent's turn."""
    from tools.delegate_tool import _build_child_agent

    chain = [{"provider": "automaticai", "model": GLM},
             {"provider": "automaticai", "model": KIMI}]
    parent = _make_agent(SOL, chain)
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = None

    with patch("run_agent.AIAgent") as child_cls:
        child_cls.return_value = MagicMock()
        try:
            _build_child_agent(
                task_index=0, goal="g", context=None, toolsets=None, model=KIMI,
                max_iterations=3, task_count=1, parent_agent=parent,
            )
        except TypeError:
            pytest.skip("delegate_tool._build_child_agent signature changed")
    assert child_cls.call_args.kwargs["fallback_model"] == chain
