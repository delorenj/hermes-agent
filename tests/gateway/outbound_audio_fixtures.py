"""Synthetic transport and audio only; no Telegram connection or private media."""
from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


def operator_event(**changes):
    source_changes = changes.pop("source", {})
    source = SessionSource(**{**dict(
        platform=Platform.TELEGRAM, chat_id="101", user_id="101", chat_type="dm"), **source_changes})
    return MessageEvent(**{**dict(text="reply", source=source, message_id="7"), **changes})


class FakeBot:
    def __init__(self, bot_id=501):
        self.id, self.sent = bot_id, []

    async def send_media(self, **kwargs):
        label = next(key for key in ("voice", "audio", "document") if key in kwargs)
        self.sent.append((label, kwargs[label].read(), kwargs))
        return SimpleNamespace(
            message_id=len(self.sent), date=datetime.now(UTC),
            chat=SimpleNamespace(id=int(kwargs["chat_id"]), type="private"),
            from_user=SimpleNamespace(id=self.id, is_bot=True))

    send_voice = send_audio = send_document = send_media


def make_adapter(tmp_path, monkeypatch, *, enabled=True, bot_id=501):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.platforms.telegram import adapter as telegram
    monkeypatch.setattr(telegram, "_probe_voice_duration_seconds", lambda path: 1)
    extra = {"outbound_audio_spool": {
        "enabled": enabled, "allowed_chat_ids": [101], "dir": str(tmp_path / "queue")}}
    adapter = TelegramAdapter(PlatformConfig(enabled=True, extra=extra))
    adapter._bot = FakeBot(bot_id)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="text"))
    return adapter
