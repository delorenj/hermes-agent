"""An availability-probe stub must never reach the aux client cache.

Repro (fleet, 2026-09-30): ``check_vision_requirements()`` resolves the vision
client inside ``aux_probe_mode()``.  With a pinned ``auxiliary.vision``
provider the resolution goes through ``_get_cached_client``, which stored the
``_AuxProbeClientStub`` directly in ``_client_cache`` (bypassing
``_store_cached_client``'s stub guard).  The next probe got a cache hit and
blew up on the stub (vision/browser_vision tools randomly gated off at
startup: True, False, False), and a real ``resolve_vision_provider_client()``
raised "_AuxProbeClientStub used as a real client".
"""

import pytest
import yaml

GATEWAY = "https://api.example-gateway.test/v1"


@pytest.fixture
def gateway_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GW_TEST_KEY", "gw-test-key-not-a-secret")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {
            "provider": "automaticai",
            "default": "gw/personal/glm-5.3",
            "api_mode": "chat_completions",
        },
        "providers": {
            "automaticai": {
                "name": "AutomaticAI",
                "api": GATEWAY,
                "key_env": "GW_TEST_KEY",
                "default_model": "gw/personal/glm-5.3",
                "api_mode": "chat_completions",
                "models": ["gw/personal/glm-5.3", "gw/personal/glm-5.3-flash"],
            },
        },
        "auxiliary": {
            "vision": {"provider": "automaticai", "model": "gw/personal/glm-5.3-flash"},
        },
    }))
    import agent.auxiliary_client as aux

    with aux._client_cache_lock:
        aux._client_cache.clear()
    yield home
    with aux._client_cache_lock:
        aux._client_cache.clear()


def _cached_stubs():
    import agent.auxiliary_client as aux

    with aux._client_cache_lock:
        return [
            key for key, entry in aux._client_cache.items()
            if isinstance(entry[0], aux._AuxProbeClientStub)
        ]


def test_probe_resolution_then_real_resolution_returns_real_client(gateway_home):
    import agent.auxiliary_client as aux

    with aux.aux_probe_mode():
        _provider, probe_client, _model = aux.resolve_vision_provider_client()
    assert isinstance(probe_client, aux._AuxProbeClientStub)
    assert _cached_stubs() == []

    provider, client, model = aux.resolve_vision_provider_client()
    assert client is not None
    assert not isinstance(client, aux._AuxProbeClientStub)
    assert GATEWAY in str(client.base_url)
    assert model == "gw/personal/glm-5.3-flash"


def test_check_vision_requirements_is_stable(gateway_home):
    from tools.vision_tools import check_vision_requirements

    results = [check_vision_requirements() for _ in range(5)]
    assert results == [True] * 5
    assert _cached_stubs() == []
