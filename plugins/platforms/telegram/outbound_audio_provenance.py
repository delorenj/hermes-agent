"""Trusted ``native_reply`` provenance for the outbound audio spool, derived only from the inbound turn.

The spool (``outbound_audio_spool``) admits a send only when its metadata carries
``outbound_audio_provenance == "native_reply"``. This module is the ONE place that sets it, and only for
gateway reply paths answering a genuine inbound Telegram private message:

* the event is a real platform message: not ``internal`` (synthetic/background notifications, plugin
  injections, Auto/Bloodbank memo or other uncorrelated origins), gateway control allowed, with a
  triggering ``message_id``;
* its source is Telegram, ``chat_type == "dm"``, a positive integer ``chat_id`` equal to the author's
  ``user_id`` (a private chat; DM topics included), not authored by a bot, and listed in the adapter's
  spool ``allowed_chat_ids``;
* the delivery adapter is a Telegram adapter whose spool is enabled.

Callers start from metadata the gateway built itself; any marker already present (e.g. copied from
user- or tool-influenced data) is stripped first, so only this function can produce it. The spool
re-checks the delivered message (private chat, allowlist, bot identity, freshness) after the send, so
this marker is necessary, never sufficient. Failure anywhere means "no marker": the send is untouched.
"""
from __future__ import annotations

from typing import Any, Optional

from plugins.platforms.telegram.outbound_audio_spool import ADMITTED_PROVENANCE, PROVENANCE_KEY


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def _positive_int(value: Any) -> Optional[int]:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() and text[0] != "0" else None


def trusted_reply_source(adapter: Any, source: Any, *, message_id: Any, internal: bool = False,
                         allow_gateway_control: bool = True) -> bool:
    """True when a reply to ``source`` may be marked: a genuine allowlisted operator-DM Telegram turn."""
    if internal or allow_gateway_control is not True or source is None or not message_id:
        return False
    spool = getattr(adapter, "_outbound_audio_spool", None)
    settings = getattr(spool, "settings", None)
    if settings is None or not getattr(settings, "enabled", False):
        return False
    if _platform_value(getattr(source, "platform", None)) != "telegram" or getattr(source, "chat_type", None) != "dm":
        return False
    if getattr(source, "is_bot", False):
        return False
    chat_id = _positive_int(getattr(source, "chat_id", None))
    if chat_id is None or chat_id != _positive_int(getattr(source, "user_id", None)):
        return False
    return chat_id in settings.allowed_chat_ids


def native_reply_metadata(adapter: Any, event: Any, metadata: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """``metadata`` with the marker set iff ``event`` is a trusted operator-DM turn; otherwise a copy with
    any pre-existing marker removed (``None`` stays ``None`` when there was nothing to strip)."""
    trusted = event is not None and trusted_reply_source(
        adapter, getattr(event, "source", None),
        message_id=getattr(event, "message_id", None) or getattr(getattr(event, "source", None), "message_id", None),
        internal=bool(getattr(event, "internal", False)),
        allow_gateway_control=getattr(event, "allow_gateway_control", True))
    return _with_marker(metadata, trusted)


def native_reply_metadata_for_source(adapter: Any, source: Any, event_message_id: Any,
                                     metadata: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Same gate for background-task completions, which keep the originating source + trigger id."""
    return _with_marker(metadata, trusted_reply_source(adapter, source, message_id=event_message_id))


def _with_marker(metadata: Optional[dict[str, Any]], trusted: bool) -> Optional[dict[str, Any]]:
    if metadata is None and not trusted:
        return None
    out = {k: v for k, v in (metadata or {}).items() if k != PROVENANCE_KEY}
    if trusted:
        out[PROVENANCE_KEY] = ADMITTED_PROVENANCE
    return out
