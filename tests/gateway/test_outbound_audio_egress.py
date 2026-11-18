"""HOLOC-26: real gateway egress seams preserve native operator provenance."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageType
from gateway.run import GatewayRunner
from gateway.turn_context import TurnContext
from tests.gateway.outbound_audio_fixtures import make_adapter, operator_event

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_base_attachment_path_uses_the_original_event(tmp_path, monkeypatch, internal):
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"base-egress")
    results = []
    await adapter._deliver_media_attachments(
        operator_event(internal=internal), [(str(audio), True)], [], force_document_attachments=False,
        human_delay=0, metadata={"notify": True}, record_delivery=results.append)
    assert len(results) == 1 and results[0].success
    assert len(adapter._outbound_audio_spool.pending()) == (0 if internal else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_stream_and_queued_turn_egress_do_not_reconstruct_trust(tmp_path, monkeypatch, internal):
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"queued-egress")
    event = operator_event(internal=internal)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._record_room_silence_decision = AsyncMock()
    runner._run_agent_stream_confirmed_final_delivery = lambda *a, **kw: False
    runner._is_intentional_silence = lambda *a: False
    runner._pop_post_delivery_callback = lambda *a: None
    ctx = TurnContext(source=event.source, reply_event=event, event_message_id="7")
    ctx._status_thread_metadata = {"notify": True}
    result = {"final_response": f"MEDIA:{audio}", "failed": False}
    await runner._run_agent_deliver_first_response(ctx, adapter, result, result, None)
    assert len(adapter._bot.sent) == 1
    assert len(adapter._outbound_audio_spool.pending()) == (0 if internal else 1)
    assert result["already_sent"] and result["media_already_delivered"]


@pytest.mark.asyncio
async def test_queue_without_original_event_is_not_native_provenance(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"uncorrelated")
    runner = GatewayRunner.__new__(GatewayRunner)
    assert await runner._deliver_queued_first_response(
        f"MEDIA:{audio}", operator_event().source, adapter, event_message_id="7")
    assert len(adapter._bot.sent) == 1 and adapter._outbound_audio_spool.pending() == []


@pytest.mark.asyncio
async def test_voice_reply_egress_carries_operator_provenance(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"voice-egress")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
    runner._get_guild_id = lambda event: None
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._thread_metadata_for_source = lambda *a: {}
    await runner._deliver_voice_reply(operator_event(), [str(audio)])
    assert len(adapter._bot.sent) == 1 and len(adapter._outbound_audio_spool.pending()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
async def test_auto_tts_never_fires_for_text_or_duplicates_captioned_audio(tmp_path, monkeypatch, voice):
    adapter = make_adapter(tmp_path, monkeypatch)
    adapter.config.typing_indicator = False
    adapter._auto_tts_default = True
    adapter._message_handler = AsyncMock(return_value="One reply.")
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"auto-tts")
    adapter._synthesize_auto_tts = AsyncMock(return_value=([str(audio)], None))
    event = operator_event(message_type=MessageType.VOICE if voice else MessageType.TEXT)
    await adapter._process_message_background(event, "test-operator-lane")
    if voice:
        assert adapter._synthesize_auto_tts.await_count == 1
        assert len(adapter._bot.sent) == 1 and adapter.send.await_count == 0
        assert len(adapter._outbound_audio_spool.pending()) == 1 and not audio.exists()
    else:
        adapter._synthesize_auto_tts.assert_not_awaited()
        assert adapter.send.await_count == 1 and not adapter._bot.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("operator,internal", [(True, False), (False, False), (True, True)])
async def test_actual_background_task_uses_its_originating_operator_source(tmp_path, monkeypatch, operator, internal):
    import run_agent
    from gateway import run
    adapter = make_adapter(tmp_path, monkeypatch)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"background-egress")

    class Agent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, **kwargs):
            return {"final_response": f"MEDIA:{audio}", "messages": []}

    monkeypatch.setattr(run_agent, "AIAgent", Agent)
    monkeypatch.setattr(run, "_load_gateway_config", dict)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._provider_routing, runner._session_db = {}, None
    runner._profile_scope_for_source = lambda source: nullcontext()
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda *a: {}
    runner._resolve_session_agent_runtime = lambda **kw: ("test-model", {"api_key": "test-only"})
    runner._resolve_turn_toolsets = lambda *a: ([], [])
    runner._resolve_session_reasoning_config = lambda **kw: {}
    runner._resolve_session_service_tier = lambda **kw: None
    runner._resolve_turn_agent_config = lambda *a: {"model": "test-model", "runtime": {}}
    runner._refresh_fallback_model = lambda: None
    runner._cleanup_agent_resources = lambda agent: None

    async def executor(fn):
        return fn()

    runner._run_in_executor_with_context = executor
    event = operator_event(internal=internal, source={} if operator else {"user_id": "202", "chat_type": "group"})
    await runner._run_background_task("Make audio", event.source, "test-bg", None, reply_event=event)
    assert len(adapter._bot.sent) == 1
    assert len(adapter._outbound_audio_spool.pending()) == (1 if operator and not internal else 0)
