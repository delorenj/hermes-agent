"""Approval prompts raised in a chat nobody reads go to the profile's human home channel.

A turn's approval prompt is rendered into the chat the turn came from. For a machine lane that is
nowhere: a Bloodbank command turn's ``send()`` only records progress, an A2A peer ignores non-final
sends. The prompt "sent" fine, nobody saw it, and the command was BLOCKED when ``approvals.timeout``
ran out (earshot-pm on EARS-59, 2026-11-25). Adapters declare that with
``BasePlatformAdapter.answers_approvals = False``; this module finds where a person can answer
instead and keeps the answer bound to the ORIGINAL session:

* the target is the turn's own profile's home channel on an adapter that answers approvals, else the
  gateway host's (launch profile's) home channel; relay-fronted homes are skipped (no buttons, no
  typed answer routing);
* the card's buttons carry the origin ``session_key``, so a tap resolves the waiting turn;
* a typed ``/approve`` / ``/deny`` in that home chat finds the rerouted prompt through
  :func:`rerouted_session_for` (``gateway/slash_commands.py``);
* no target at all raises :class:`tools.approval_gateway_wait.ApprovalUndeliverable`, which the turn
  reports as BLOCKED with the reason, instead of waiting out a timeout nobody can stop;
* the wait window is ``approvals.rerouted_timeout`` (declared by the notifier, see
  ``gateway/run_turn_runner_approval_transport.py``).

The approval decision itself is untouched: who may answer, what an answer means and fail-closed on
silence all stay with ``tools/approval*.py``. Only where the prompt goes, and how the answer finds its
way back, change here.
"""

from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


def answers_approvals(adapter: Any) -> bool:
    """Whether a human reads *adapter*'s chats and can answer a prompt there. Read on the CLASS:
    MagicMock instances auto-create attributes, so only a real ``False`` declaration counts."""
    if adapter is None:
        return True
    return getattr(type(adapter), "answers_approvals", True) is not False


@dataclass
class HumanApprovalTarget:
    """Where a rerouted prompt is rendered, and what the human is told about its origin."""

    adapter: Any
    chat_id: str
    metadata: Optional[dict] = None
    platform: Any = None
    profile: Optional[str] = None  # the home's profile (None: the gateway host's own)
    origin_profile: Optional[str] = None
    origin_platform: str = ""

    @property
    def platform_value(self) -> str:
        return str(getattr(self.platform, "value", self.platform) or "")


def _platform_label(source: Any) -> str:
    platform = getattr(source, "platform", None)
    value = str(getattr(platform, "value", platform) or "")
    return value or "unknown"


def _profile_label(profile: Optional[str]) -> str:
    return profile or "default"


def resolve_human_target(runner: Any, source: Any) -> Optional[HumanApprovalTarget]:
    """The home channel a prompt from *source*'s turn should go to: the turn's own profile's first,
    else the gateway host's. ``None`` when no served profile offers one."""
    served = getattr(runner, "_served_home_channel_transports", None)
    if not callable(served):
        return None
    origin_profile = getattr(source, "profile", None) or None
    own: list[HumanApprovalTarget] = []
    host: list[HumanApprovalTarget] = []
    try:
        homes = list(served())
    except Exception:
        logger.warning("Approval reroute: listing home channels failed", exc_info=True)
        return None
    for profile, platform, _cfg, home, transport in homes:
        if transport is None or getattr(transport, "is_relay", False):
            continue
        adapter = getattr(transport, "adapter", None)
        if adapter is None or not answers_approvals(adapter):
            continue
        metadata = None
        build_metadata = getattr(runner, "_thread_metadata_for_target", None)
        if callable(build_metadata):
            try:
                metadata = build_metadata(platform, home.chat_id, home.thread_id, adapter=adapter)
            except Exception:
                logger.debug("Approval reroute: thread metadata for the home channel failed", exc_info=True)
        target = HumanApprovalTarget(
            adapter=adapter, chat_id=str(home.chat_id), metadata=metadata, platform=platform,
            profile=profile, origin_profile=origin_profile, origin_platform=_platform_label(source))
        if (profile or None) == origin_profile:
            own.append(target)
        elif profile is None:
            host.append(target)
    picked = (own or host or [None])[0]
    return picked


def undeliverable_reason(source: Any) -> str:
    """The agent-readable reason a prompt from *source* could reach nobody."""
    return (
        f"this turn came in over {_platform_label(source)}, which no person reads, and profile "
        f"'{_profile_label(getattr(source, 'profile', None))}' has no human home channel the gateway "
        "can reach (and neither does the gateway host). Nobody was asked. Tell the operator this "
        "command needs approval and that the profile needs a home channel (run /sethome in its "
        "Telegram DM, or set TELEGRAM_HOME_CHANNEL)."
    )


def rerouted_reason(target: HumanApprovalTarget, reason: str) -> str:
    """The card's "why it was flagged" line, prefixed with where the request really came from: the
    human sees it in a chat that did not start the turn (often another profile's bot)."""
    from agent.i18n import t
    return t("gateway.exec_approval.rerouted_reason", profile=_profile_label(target.origin_profile),
             platform=target.origin_platform, reason=reason)


# ── typed /approve and /deny in the home chat ───────────────────────────────────────────────────
# (platform value, chat id) → [(adapter weakref, origin session key, request id)]. A card button
# carries its origin session itself; a typed answer arrives in the home chat's OWN session, which
# has no pending approval, so the slash handler asks here. Entries leave when the wait settles.

_lock = threading.Lock()
_rerouted: dict[tuple[str, str], list[tuple[Any, str, str]]] = {}


def _forget(key: tuple[str, str], session_key: str, request_id: str) -> None:
    with _lock:
        rows = _rerouted.get(key)
        if not rows:
            return
        rows[:] = [row for row in rows if not (row[1] == session_key and row[2] == request_id)]
        if not rows:
            _rerouted.pop(key, None)


def track_rerouted(target: HumanApprovalTarget, session_key: str, request_id: Optional[str]) -> None:
    """Remember that *request_id* of *session_key* is shown in *target*'s chat until its wait ends."""
    if not session_key or not request_id:
        return
    from tools.approval import observe_gateway_settle
    key = (target.platform_value, str(target.chat_id))
    try:
        ref = weakref.ref(target.adapter)
    except TypeError:  # an adapter type without weakref support: hold it until the wait settles
        adapter = target.adapter
        ref = lambda: adapter
    with _lock:
        _rerouted.setdefault(key, []).append((ref, session_key, request_id))
    if not observe_gateway_settle(session_key, request_id, lambda _reason: _forget(key, session_key, request_id)):
        _forget(key, session_key, request_id)  # already settled: nothing left to answer here


def rerouted_session_for(adapter: Any, source: Any) -> Optional[str]:
    """The origin session whose rerouted prompt is waiting in *source*'s chat on *adapter*, oldest
    first, or ``None``. Only a still-blocking session counts."""
    if adapter is None or source is None:
        return None
    from tools.approval import has_blocking_approval
    key = (_platform_label(source), str(getattr(source, "chat_id", "") or ""))
    with _lock:
        rows = list(_rerouted.get(key, ()))
    for ref, session_key, _request_id in rows:
        if ref() is adapter and has_blocking_approval(session_key):
            return session_key
    return None


def _reset_for_tests() -> None:
    with _lock:
        _rerouted.clear()
