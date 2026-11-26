"""A changed profile's rescan re-pulls its external secret sources.

Secret sources (1Password, Bitwarden) hydrate once per home. Wiring a Telegram bot by
``op://`` reference into an already-served profile changes only its config; without a
per-home reset the rescan reads the stale snapshot and logs "no bot credential" until the
gateway restarts (storefront-pm, 2026-11-26).
"""
from unittest.mock import patch

import pytest

from tests.gateway.test_multiplex_hot_serve import _mkprofile, _runner


@pytest.mark.asyncio
async def test_rescan_resets_only_the_changed_profiles_secret_snapshot(tmp_path, monkeypatch):
    runner, home = _runner(tmp_path, monkeypatch)
    alpha = _mkprofile(home, "alpha", "DISCORD_BOT_TOKEN=alpha-token\n")
    _mkprofile(home, "beta", "DISCORD_BOT_TOKEN=beta-token\n")
    with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
        await runner._start_secondary_profile_adapters()
        with patch("hermes_cli.env_loader.reset_secret_source_cache") as reset:
            (alpha / ".env").write_text("DISCORD_BOT_TOKEN=alpha-token\n# wired\n", encoding="utf-8")
            assert (await runner.reconcile_served_profiles())["rescanned"] == ["alpha"]
        homes = [call.args[0] for call in reset.call_args_list]
        assert [p.name for p in homes] == ["alpha"]
