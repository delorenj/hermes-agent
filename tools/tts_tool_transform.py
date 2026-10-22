"""``transform_tts_text`` plugin hook for ``tools.tts_tool``: lets a plugin rewrite the whole
spoken text (e.g. a voice persona) after normalization and before chunking.

Fires once per ``text_to_speech_tool`` call on the whole response, never per chunk; the
sentence-level streaming path (``tools.tts_streaming``) cannot rewrite a whole response and is
out of scope. Fail-open: a missing hook, a bad return value, an exception or a timeout all
leave the original text untouched. The hook is NOT in the generic short-callback timeout set
(it may wrap a multi-second LLM call), so it carries its own ``tts.transform_timeout_seconds``.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any, Optional

from tools.tts_command_provider import (
    BUILTIN_TTS_PROVIDERS, _get_named_provider_config, _get_provider_section)

logger = logging.getLogger("tools.tts_tool")

HOOK_NAME = "transform_tts_text"
DEFAULT_TRANSFORM_TIMEOUT_SECONDS = 45.0


def _config_voice(section: Any) -> Optional[str]:
    voice = section.get("voice") if isinstance(section, dict) else None
    return voice.strip() if isinstance(voice, str) and voice.strip() else None


def resolve_configured_voice(provider: str, tts_config: dict[str, Any]) -> Optional[str]:
    """The voice Hermes would use for *provider*: ``tts.<provider>.voice`` (or the named
    ``tts.providers.<provider>`` block for non-built-ins), then top-level ``tts.voice``; None if unset."""
    key = (provider or "").lower().strip()
    cfg = tts_config if isinstance(tts_config, dict) else {}
    if key in BUILTIN_TTS_PROVIDERS:
        section = _get_provider_section(cfg, key)
    else:
        section = _get_named_provider_config(cfg, key)
    return _config_voice(section) or _config_voice(cfg)


def _resolve_timeout(tts_config: dict[str, Any]) -> float:
    raw = tts_config.get("transform_timeout_seconds") if isinstance(tts_config, dict) else None
    try:
        timeout = float(raw) if raw is not None else DEFAULT_TRANSFORM_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        logger.warning("tts.transform_timeout_seconds is not a number; using default %gs",
                       DEFAULT_TRANSFORM_TIMEOUT_SECONDS)
        return DEFAULT_TRANSFORM_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_TRANSFORM_TIMEOUT_SECONDS


def _invoke_bounded(timeout: float, **kwargs: Any) -> Optional[list]:
    """``invoke_hook`` on a daemon worker with a wall-clock cap; None on timeout (worker abandoned,
    never joined — same stance as the generic hook timeout, #6622). Runs under the caller's
    contextvars so the profile scope (HERMES_HOME override, secrets) follows the call."""
    from hermes_cli.plugins import invoke_hook
    outcome: dict[str, Any] = {}
    done = threading.Event()
    ctx = contextvars.copy_context()

    def _runner() -> None:
        try:
            outcome["value"] = ctx.run(invoke_hook, HOOK_NAME, **kwargs)
        except BaseException as exc:  # SystemExit from a plugin must not kill the worker silently
            outcome["exc"] = exc
        finally:
            done.set()

    threading.Thread(target=_runner, name="hermes-hook-transform_tts_text", daemon=True).start()
    if not done.wait(timeout=timeout):
        logger.warning("Hook '%s' timed out after %gs - speaking the original text", HOOK_NAME, timeout)
        return None
    if "exc" in outcome:
        raise outcome["exc"]
    return outcome.get("value")


def apply_tts_text_transform(
    text: str, provider: str, tts_config: dict[str, Any], platform: Optional[str] = None,
) -> str:
    """Return the text to speak: the first non-empty ``str`` a ``transform_tts_text`` callback
    returns (re-normalized), else *text* unchanged. Never raises."""
    try:
        from hermes_cli.plugins import has_hook
        if not has_hook(HOOK_NAME):  # zero-overhead path: no plugin registered the hook
            return text
        voice = resolve_configured_voice(provider, tts_config)
        results = _invoke_bounded(
            _resolve_timeout(tts_config), text=text, provider=provider, voice=voice,
            platform=platform or None)
    except (Exception, SystemExit) as exc:
        logger.warning("%s hook failed (%s) - speaking the original text", HOOK_NAME, exc)
        return text
    for result in results or ():
        if not isinstance(result, str) or not result.strip():
            continue
        try:
            from tools.tts_text_normalize import prepare_spoken_text
            rewritten = prepare_spoken_text(result, max_chars=None)
        except Exception:
            rewritten = result.strip()
        if not rewritten:
            continue
        logger.info("TTS text transformed by %s hook (provider=%s, voice=%s): %d -> %d chars",
                    HOOK_NAME, provider, voice, len(text), len(rewritten))
        return rewritten
    return text
