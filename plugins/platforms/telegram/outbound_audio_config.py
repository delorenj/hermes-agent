"""Read only the audio configuration; never load plugins, env files or secret references.

Also executable by the shell gate without importing the Telegram package. Conflicting
configuration shapes fail closed rather than letting an inherited true override a false.
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

_KEYS = ("outbound_audio_spool", "outbound_audio_drain")


def both_enabled(extra: Any) -> bool:
    return isinstance(extra, dict) and all(
        isinstance(extra.get(key), dict) and extra[key].get("enabled") is True for key in _KEYS)


def audio_extra(config: Any) -> dict:
    if not isinstance(config, dict):
        raise ValueError("audio config must be a mapping")
    extra = {}
    platforms = config.get("platforms") or {}
    gateway = config.get("gateway") or {}
    blocks = [config.get("telegram"), platforms.get("telegram"),
              (gateway.get("platforms") or {}).get("telegram")]
    for block in blocks:
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ValueError("Telegram config must be a mapping")
        nested = block.get("extra", {})
        if not isinstance(nested, dict):
            raise ValueError("Telegram extra must be a mapping")
        for candidate in (block, nested):
            for key in _KEYS:
                if key not in candidate:
                    continue
                value = candidate[key]
                if not isinstance(value, dict):
                    raise ValueError("audio settings must be a mapping")
                old = extra.get(key)
                if old is not None and old != value:
                    raise ValueError("conflicting audio config shapes")
                extra[key] = value
    return extra


def load_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    from ruamel.yaml import YAML, YAMLError

    try:
        config = YAML(typ="safe").load(path.read_text(encoding="utf-8-sig"))  # config-reader: ok -- offline pre-secret safety gate must not invoke a remote backend
    except YAMLError:
        raise ValueError("audio config cannot be parsed") from None
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("audio config must be a mapping")
    return config


def load_audio_extra(path: Path) -> dict:
    return audio_extra(load_mapping(path))


def main(argv: list[str]) -> int:
    try:
        return 0 if len(argv) == 1 and both_enabled(load_audio_extra(Path(argv[0]))) else 3
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        print("outbound-audio-drain: invalid config or YAML parser unavailable; refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
