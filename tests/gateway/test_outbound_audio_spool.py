"""HOLOC-26: operator-only admission, exact delivered handles and queue isolation."""
import concurrent.futures
import hashlib
import json
import time
from datetime import timedelta

import pytest

from plugins.platforms.telegram.outbound_audio_provenance import native_reply_metadata
from plugins.platforms.telegram.outbound_audio_spool import OutboundAudioSpool, SpoolSettings
from tests.gateway.outbound_audio_fixtures import make_adapter, operator_event

pytestmark = pytest.mark.platforms("posix")


def record(message_id=1):
    now = time.time()
    return dict(bot_id=501, chat_id=101, message_id=message_id, attachment_index=0,
                media_kind="voice", extension=".ogg", sent_at=now, expires_at=now + 900)


@pytest.mark.asyncio
@pytest.mark.parametrize("ext,label", [(".ogg", "voice"), (".mp3", "audio"), (".wav", "document")])
async def test_actual_native_send_captures_only_delivered_bytes(tmp_path, monkeypatch, ext, label):
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / ("synthetic" + ext)
    audio.write_bytes(b"synthetic-outbound-only")
    metadata = native_reply_metadata(adapter, operator_event(), {"notify": True})
    result = await adapter.send_voice("101", str(audio), metadata=metadata)
    assert result.success
    assert adapter._bot.sent[0][:2] == (label, b"synthetic-outbound-only")
    spool = adapter._outbound_audio_spool
    [entry] = spool.pending()
    assert spool.read_for_drain(entry) == b"synthetic-outbound-only"
    assert entry["sha256"] == hashlib.sha256(adapter._bot.sent[0][1]).hexdigest()
    assert entry["media_kind"] == label


@pytest.mark.asyncio
async def test_transcoded_handle_is_captured_before_cleanup(tmp_path, monkeypatch):
    from gateway.platforms import base
    adapter = make_adapter(tmp_path, monkeypatch)
    original, transcoded = tmp_path / "original.mp3", tmp_path / "converted.ogg"
    original.write_bytes(b"original")
    transcoded.write_bytes(b"delivered-ogg")
    monkeypatch.setattr(base, "transcode_to_ogg_opus", lambda path: str(transcoded))
    metadata = native_reply_metadata(adapter, operator_event(), None)
    result = await adapter.send_voice("101", str(original), metadata=metadata, is_voice=True)
    assert result.success and not transcoded.exists()
    [entry] = adapter._outbound_audio_spool.pending()
    assert adapter._outbound_audio_spool.read_for_drain(entry) == b"delivered-ogg"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"internal": True}, {"allow_gateway_control": False}, {"message_id": None},
    {"source": {"chat_type": "group"}}, {"source": {"user_id": "202"}},
    {"source": {"platform": "discord"}}, {"source": {"chat_id": "-101"}},
])
async def test_untrusted_origin_can_send_but_cannot_capture(tmp_path, monkeypatch, changes):
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"synthetic")
    event = operator_event(**changes)
    metadata = native_reply_metadata(adapter, event, {"outbound_audio_provenance": "native_reply"})
    assert "outbound_audio_provenance" not in metadata
    assert (await adapter.send_voice("101", str(audio), metadata=metadata)).success
    assert adapter._outbound_audio_spool.pending() == []


@pytest.mark.parametrize("field,value", [
    ("media_kind", []), ("sent_at", 10**309), ("chat_id", 202),
    ("expires_at", float("inf")), ("sha256", []),
])
def test_bad_valid_json_cannot_poison_other_entries(tmp_path, field, value):
    spool = OutboundAudioSpool(SpoolSettings(True, tmp_path / "queue", frozenset({101})))
    first, second = record(1), record(2)
    assert spool.store(first, b"one") and spool.store(second, b"two")
    path = spool.settings.root / (spool.entry_key(first) + ".json")
    malformed = json.loads(path.read_text())
    malformed[field] = value
    path.write_text(json.dumps(malformed))
    entries = spool.pending()
    assert [e["message_id"] for e in entries] == [2]
    assert spool.read_for_drain(entries[0]) == b"two"


def test_cross_writer_dedupe_is_bot_scoped(tmp_path):
    settings = SpoolSettings(True, tmp_path / "queue", frozenset({101}))
    entry = record()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: OutboundAudioSpool(settings).store(entry, b"same"), range(2)))
    assert sorted(results) == [False, True]
    spool = OutboundAudioSpool(settings)
    assert spool.store(dict(entry, bot_id=502), b"other-bot")
    assert {e["bot_id"] for e in spool.pending()} == {501, 502}


def test_disabled_spool_methods_do_not_touch_existing_queue(tmp_path, monkeypatch):
    spool = OutboundAudioSpool(SpoolSettings(False, tmp_path / "queue", frozenset({101})))
    monkeypatch.setattr(spool, "_locked", lambda: pytest.fail("disabled spool acquired a lock"))
    assert spool.pending() == []
    assert not spool.store(record(), b"data")
    assert spool.read_for_drain({}) is None
    assert not spool.consume({}, "irrelevant")
    spool.prune()
    assert not spool.settings.root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["group", "wrong-bot", "human-sender", "expired", "changed-source"])
async def test_delivery_ack_must_identify_a_fresh_bot_private_send(tmp_path, monkeypatch, change):
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"before")
    original = adapter._bot.send_voice

    async def send(**kwargs):
        message = await original(**kwargs)
        {
            "group": lambda: setattr(message.chat, "type", "group"),
            "wrong-bot": lambda: setattr(message.from_user, "id", 999),
            "human-sender": lambda: setattr(message.from_user, "is_bot", False),
            "expired": lambda: setattr(message, "date", message.date - timedelta(hours=1)),
            "changed-source": lambda: audio.write_bytes(b"after"),
        }[change]()
        return message

    adapter._bot.send_voice = send
    result = await adapter.send_voice(
        "101", str(audio), metadata=native_reply_metadata(adapter, operator_event(), None))
    assert result.success and adapter._outbound_audio_spool.pending() == []
