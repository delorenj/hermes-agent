"""Refuse unresolved 1Password ``op://`` references at credential read time.

A ``.env`` line like ``KIMI_API_KEY=op://Vault/Item/field`` is only a pointer.
When 1Password resolution is skipped, fails, or is clobbered by a later raw
``.env`` reload, the literal ``op://...`` string used to flow straight into an
``Authorization: Bearer`` header, and providers answered with confusing 401s
("API Key appears to be invalid", "Missing Authentication header").

Credential readers pass every value through :func:`refuse_op_ref`: an
``op://`` value is treated as absent and one WARNING per variable name is
logged. The value itself is never logged (fork tweak, delorenj/hermes-agent).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

OP_REF_PREFIX = "op://"

_WARNED: set[str] = set()
_WARNED_LOCK = threading.Lock()


def is_unresolved_op_ref(value: object) -> bool:
    """True when ``value`` is a string that is still an ``op://`` reference."""
    return isinstance(value, str) and value.strip().startswith(OP_REF_PREFIX)


def refuse_op_ref(name: str, value: Optional[str], *, reader: str = "") -> Optional[str]:
    """Return ``value`` unchanged unless it is an unresolved ``op://`` reference.

    For an ``op://`` value return ``""`` and log a single WARNING naming
    ``name`` (never the value) the first time that variable is refused in
    this process.
    """
    if not is_unresolved_op_ref(value):
        return value
    with _WARNED_LOCK:
        first = name not in _WARNED
        _WARNED.add(name)
    if first:
        logger.warning(
            "%s holds an unresolved 1Password reference (op://...), not a "
            "credential; treating it as unset%s. Check secrets.onepassword "
            "in config.yaml and the 1Password service-account token.",
            name,
            f" ({reader})" if reader else "",
        )
    return ""


def _reset_warned_for_tests() -> None:
    with _WARNED_LOCK:
        _WARNED.clear()
