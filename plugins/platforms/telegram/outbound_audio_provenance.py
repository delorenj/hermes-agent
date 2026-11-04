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

from plugins.platforms.telegram.outbound_audio_spool import (
    ADMITTED_PROVENANCE, AUDIO_EXTENSIONS, CRON_PROVENANCE, PROVENANCE_KEY,
)

CRON_REPLY_FLAG = "_outbound_audio_reply"


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def _positive_int(value: Any) -> Optional[int]:
    text = str(value).strip() if value is not None else ""
    return int(text) if 0 < len(text) <= 20 and text.isascii() and text.isdigit() and text[0] != "0" else None


def trusted_reply_source(adapter: Any, source: Any, *, message_id: Any, internal: bool = False,
                         allow_gateway_control: bool = True) -> bool:
    """True when a reply to ``source`` may be marked: a genuine allowlisted operator-DM Telegram turn."""
    if internal or allow_gateway_control is not True or source is None or not message_id:
        return False
    spool = getattr(adapter, "_outbound_audio_spool", None)
    settings = getattr(spool, "settings", None)
    if settings is None or not getattr(settings, "enabled", False):
        return False
    if _platform_value(getattr(adapter, "platform", None)) != "telegram":
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


def _with_marker(metadata: Optional[dict[str, Any]], trusted: bool) -> Optional[dict[str, Any]]:
    if metadata is None and not trusted:
        return None
    out = {k: v for k, v in (metadata or {}).items() if k != PROVENANCE_KEY}
    if trusted:
        out[PROVENANCE_KEY] = ADMITTED_PROVENANCE
    return out


def stamp_cron_reply(job: dict, response: str, execution_id: str | None, config: dict) -> None:
    """Correlate explicit audio from this successful run, not text-only TTS or old job metadata."""
    from pathlib import Path
    from plugins.platforms.telegram.outbound_audio_config import audio_extra
    from gateway.platforms.base import BasePlatformAdapter

    job.pop(CRON_REPLY_FLAG, None)
    try:
        spool = audio_extra(config).get("outbound_audio_spool") or {}
    except (TypeError, ValueError, AttributeError):
        return
    if spool.get("enabled") is not True or not execution_id or job.get("no_agent"):
        return
    origin = job.get("origin")
    if (not isinstance(origin, dict) or origin.get("platform") != "telegram"
            or _positive_int(origin.get("chat_id")) is None):
        return
    media, _ = BasePlatformAdapter.extract_media(response)
    paths = [path for path, _ in media if Path(path).suffix.lower() in AUDIO_EXTENSIONS]
    if paths:
        job[CRON_REPLY_FLAG] = {"execution_id": execution_id, "job_id": job["id"], "paths": paths}


def cron_reply_metadata(adapter, job, chat_id, path, metadata):
    """Only explicit run-correlated audio delivered back to an allowlisted Telegram origin DM."""
    from types import SimpleNamespace

    proof, origin = job.get(CRON_REPLY_FLAG), job.get("origin")
    trusted = False
    if isinstance(proof, dict) and isinstance(origin, dict):
        source = SimpleNamespace(platform=origin.get("platform"), chat_type="dm",
                                 chat_id=origin.get("chat_id"), user_id=origin.get("chat_id"))
        trusted = (
            proof.get("execution_id") == job.get("execution_id") and bool(job.get("execution_id"))
            and proof.get("job_id") == job.get("id") and isinstance(proof.get("paths"), list)
            and str(path) in proof["paths"] and str(chat_id) == str(origin.get("chat_id"))
            and trusted_reply_source(adapter, source, message_id=job.get("execution_id")))
    out = _with_marker(metadata, False)
    if trusted:
        out = dict(out or {}, **{PROVENANCE_KEY: CRON_PROVENANCE})
    return out
