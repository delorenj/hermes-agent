"""Validated process-local model routing overrides for gateway startup.

The override deliberately stores an environment variable *name*, never the
credential value.  The value is resolved inside the active secret scope when a
turn is built, which keeps credentials out of argv, logs, config files, and
process metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional
from urllib.parse import urlsplit


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_API_MODES = frozenset(
    {
        "anthropic_messages",
        "bedrock_converse",
        "chat_completions",
        "codex_app_server",
        "codex_responses",
    }
)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


@dataclass(frozen=True)
class GatewayModelOverride:
    """Non-secret route metadata supplied to ``hermes gateway run``."""

    model: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_mode: Optional[str] = None
    key_env: Optional[str] = None

    @classmethod
    def build(
        cls,
        *,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        api_mode: Optional[str] = None,
        key_env: Optional[str] = None,
    ) -> Optional["GatewayModelOverride"]:
        values = {
            "model": _clean(model),
            "provider": _clean(provider),
            "base_url": _clean(base_url),
            "api_mode": _clean(api_mode),
            "key_env": _clean(key_env),
        }
        if not any(values.values()):
            return None

        key_name = values["key_env"]
        if key_name and not _ENV_NAME_RE.fullmatch(key_name):
            raise ValueError(
                "--key-env must be an environment variable name, not a value"
            )

        mode = values["api_mode"]
        if mode and mode not in _API_MODES:
            raise ValueError(
                "--api-mode must be one of: " + ", ".join(sorted(_API_MODES))
            )

        url = values["base_url"]
        if url:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "--base-url must be an http(s) URL without credentials, "
                    "query parameters, or a fragment"
                )

        return cls(**values)
