"""Explicit fixture config is flattened; malformed neighbors never block valid recovery."""
import json

import pytest

from plugins.platforms.telegram import outbound_audio_drain as drain
from tests.gateway.test_outbound_audio_drain import MemoryS3, seeded
from tests.gateway.test_outbound_audio_spool import record


@pytest.mark.parametrize("container", ["telegram", "platforms", "gateway"])
def test_whole_config_is_rejected_before_client_or_queue(tmp_path, monkeypatch, container):
    path = tmp_path / "whole.json"
    path.write_text(json.dumps({container: {}}))
    monkeypatch.setattr(drain, "build_drain", lambda *a: pytest.fail("must reject before IO"))
    assert drain.main(["--once", "--config", str(path)]) == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_flattened_extra_reaches_factory_without_implicit_enablement(tmp_path, monkeypatch, enabled):
    extra = {key: {"enabled": enabled} for key in ("outbound_audio_spool", "outbound_audio_drain")}
    path = tmp_path / "extra.json"
    path.write_text(json.dumps(extra))
    seen = []

    def factory(config, home):
        seen.append(config)

    monkeypatch.setattr(drain, "build_drain", factory)
    assert drain.main(["--once", "--config", str(path)]) == (2 if enabled else 0)
    assert seen == [extra]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("raw", ['{', '[]', 'null', '{"sent_at":[]}', '"scalar"'])
def test_corrupt_metadata_cannot_poison_drain_of_valid_sibling(tmp_path, raw):
    spool = seeded(tmp_path)
    assert spool.store(record(2), b"second-synthetic")
    first = next(entry for entry in spool.pending() if entry["message_id"] == 1)
    (spool.settings.root / (spool.entry_key(first) + ".json")).write_text(raw)
    client = MemoryS3()
    result = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=True), client).drain_once()
    assert result.uploaded == 1
    assert client.calls[0][1]["Body"] == b"second-synthetic"
    assert spool.pending() == []
