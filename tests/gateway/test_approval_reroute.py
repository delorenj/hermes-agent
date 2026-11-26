"""An approval prompt raised in a chat nobody reads goes to the profile's human home channel.

Regression for EARS-59 (2026-11-25): a Bloodbank command turn's prompt was "sent" into an adapter whose
``send()`` delivers nothing, so it timed out unseen after ``approvals.timeout`` and the command was
BLOCKED. The contract pinned here: the prompt reaches a human, an answer there resolves the ORIGINAL
turn's wait, no reachable human fails loudly at once, and the decision itself is never made for them.
"""
import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

import gateway.run  # load bootstrap before the per-test home I/O guard
from gateway.approval_reroute import (
    answers_approvals, rerouted_session_for, resolve_human_target, _reset_for_tests)
from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.run_turn_runner_approval_transport import approval_transport
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from tools import approval, approval_context
from tools.approval_gateway_wait import ApprovalUndeliverable, _await_gateway_decision

ORIGIN = "agent:main:bloodbank:origin"
HOME_CHAT = "424242"
COMMAND = {"command": "rm -rf /tmp/x", "description": "recursive delete", "pattern_key": "k",
           "pattern_keys": ["k"]}


class MachineLane:
    """Stands in for BloodbankAdapter / A2AAdapter: sends succeed, nobody reads them."""
    answers_approvals = False

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)
        return SimpleNamespace(success=True, message_id="void")


class HumanChat:
    """Stands in for the Telegram adapter: renders a button card and records what it carried."""

    def __init__(self):
        self.cards = []
        self.texts = []

    async def send_exec_approval(self, **kwargs):
        self.cards.append(kwargs)
        # What every real card does once it shows the whole command: lift the Deny-only mark.
        approval.restrict_gateway_approval_card_to_deny(
            kwargs["session_key"], kwargs.get("request_id"), restricted=False)
        return SimpleNamespace(success=True, message_id="card-1")

    async def send(self, chat_id, text, metadata=None):
        self.texts.append((chat_id, text))
        return SimpleNamespace(success=True, message_id="msg")

    async def edit_message(self, *args, **kwargs):
        return SimpleNamespace(success=True)


def _home(chat_id=HOME_CHAT, thread_id=None):
    return SimpleNamespace(chat_id=chat_id, thread_id=thread_id)


def _runner(*homes):
    """homes: (profile, adapter) pairs, in the order the gateway lists them."""
    rows = [(profile, Platform.TELEGRAM, None, _home(), SimpleNamespace(adapter=adapter, is_relay=False))
            for profile, adapter in homes]
    return SimpleNamespace(_served_home_channel_transports=lambda: rows,
                           _thread_metadata_for_target=lambda *a, **k: None)


def _source(profile="earshot-pm"):
    # Any messaging surface will do: what makes the lane readerless is the ADAPTER's declaration.
    return SessionSource(platform=Platform.MATTERMOST, chat_id="bb", chat_type="dm", user_id="krebs",
                         profile=profile)


def _sync_schedule(monkeypatch):
    def schedule(self, coro, *args):
        fut = Future()
        fut.set_result(asyncio.run(coro))
        return fut
    monkeypatch.setattr(TurnRunner, "_schedule", schedule)


def _notifier(adapter, runner, source, session_key=ORIGIN):
    ctx = TurnContext(session_key=session_key, source=source, _status_adapter_pinned=adapter,
                      _status_chat_id="bb")
    return approval_transport(TurnRunner(runner, ctx))


@pytest.fixture(autouse=True)
def _clean():
    _reset_for_tests()
    yield
    _reset_for_tests()
    approval.clear_session(ORIGIN)


def test_only_a_declared_machine_lane_is_unattended():
    assert answers_approvals(HumanChat()) is True
    assert answers_approvals(None) is True
    assert answers_approvals(MachineLane()) is False
    # A mock auto-creates attributes: only a real class-level False counts.
    assert answers_approvals(SimpleNamespace(answers_approvals=False)) is True


def test_profile_home_wins_over_host_home_and_relay_homes_are_skipped():
    own, host = HumanChat(), HumanChat()
    relay = SimpleNamespace(adapter=HumanChat(), is_relay=True)
    rows = [(None, Platform.TELEGRAM, None, _home("host"), SimpleNamespace(adapter=host, is_relay=False)),
            ("earshot-pm", Platform.TELEGRAM, None, _home("own"), relay),
            ("earshot-pm", Platform.TELEGRAM, None, _home("own"), SimpleNamespace(adapter=own, is_relay=False))]
    runner = SimpleNamespace(_served_home_channel_transports=lambda: rows)
    assert resolve_human_target(runner, _source("earshot-pm")).adapter is own
    # A profile with no Telegram of its own falls back to the gateway host's home.
    assert resolve_human_target(runner, _source("no-telegram-profile")).adapter is host
    # Never a channel that cannot answer.
    assert resolve_human_target(_runner(("earshot-pm", MachineLane())), _source()) is None


def test_prompt_reaches_the_home_channel_and_a_tap_resolves_the_origin_wait(monkeypatch):
    _sync_schedule(monkeypatch)
    lane, human = MachineLane(), HumanChat()
    notify = _notifier(lane, _runner((None, human)), _source()).background_notify
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(_await_gateway_decision(ORIGIN, notify, dict(COMMAND))), daemon=True)
    worker.start()
    for _ in range(200):
        if human.cards:
            break
        threading.Event().wait(0.02)
    assert lane.sent == [], "the readerless lane must never be the destination"
    card = human.cards[0]
    assert card["chat_id"] == HOME_CHAT
    assert card["session_key"] == ORIGIN, "the tap must resolve the ORIGINAL turn's wait"
    assert "earshot-pm" in card["description"], "the human must be told whose turn this is"
    # The waiter was told the rerouted window, not approvals.timeout.
    assert approval.get_pending_gateway_approval(ORIGIN)["timeout_s"] == approval_context._get_rerouted_approval_timeout()

    assert approval.resolve_gateway_approval_card(ORIGIN, "once", card["request_id"]) == 1
    worker.join(timeout=5)
    assert result.get("choice") == "once", result


def test_denying_in_the_home_channel_still_denies(monkeypatch):
    _sync_schedule(monkeypatch)
    human = HumanChat()
    notify = _notifier(MachineLane(), _runner((None, human)), _source()).background_notify
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(_await_gateway_decision(ORIGIN, notify, dict(COMMAND))), daemon=True)
    worker.start()
    for _ in range(200):
        if human.cards:
            break
        threading.Event().wait(0.02)
    approval.resolve_gateway_approval_card(ORIGIN, "deny", human.cards[0]["request_id"])
    worker.join(timeout=5)
    assert result.get("choice") == "deny", result


def test_typed_approve_in_the_home_chat_finds_the_origin_session(monkeypatch):
    _sync_schedule(monkeypatch)
    human = HumanChat()
    notify = _notifier(MachineLane(), _runner((None, human)), _source()).background_notify
    worker = threading.Thread(target=lambda: _await_gateway_decision(ORIGIN, notify, dict(COMMAND)), daemon=True)
    worker.start()
    for _ in range(200):
        if human.cards:
            break
        threading.Event().wait(0.02)
    home_source = SessionSource(platform=Platform.TELEGRAM, chat_id=HOME_CHAT, chat_type="dm", user_id="jarad")
    other_source = SessionSource(platform=Platform.TELEGRAM, chat_id="999", chat_type="dm", user_id="x")
    assert rerouted_session_for(human, home_source) == ORIGIN
    assert rerouted_session_for(human, other_source) is None, "only the chat the card is in may answer"
    assert rerouted_session_for(HumanChat(), home_source) is None, "and only through the adapter that showed it"

    approval.resolve_gateway_approval(ORIGIN, "once")
    worker.join(timeout=5)
    assert rerouted_session_for(human, home_source) is None, "a settled wait leaves nothing to answer"


def test_no_home_channel_fails_loudly_instead_of_timing_out(monkeypatch):
    _sync_schedule(monkeypatch)
    lane = MachineLane()
    notify = _notifier(lane, _runner(), _source()).background_notify
    with pytest.raises(ApprovalUndeliverable) as raised:
        notify(dict(COMMAND))
    assert "home channel" in str(raised.value) and "/sethome" in str(raised.value)

    # Through the real waiter: BLOCKED at once, with the reason in the agent-visible text.
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 60)
    started = threading.Event()
    result = {}
    worker = threading.Thread(
        target=lambda: (started.set(), result.update(_await_gateway_decision(ORIGIN, notify, dict(COMMAND)))),
        daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "an undeliverable prompt must not wait out a timeout"
    assert result["notify_failed"] is True
    assert "home channel" in result["notify_error"]
    assert lane.sent == []


def test_the_blocked_command_tells_the_agent_why_nobody_was_asked(monkeypatch):
    """Through the real gate: BLOCKED immediately, never a silent timeout, and never auto-approved."""
    _sync_schedule(monkeypatch)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    for var in ("HERMES_CRON_SESSION", "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", ORIGIN)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "mattermost")
    approval.register_gateway_notify(ORIGIN, _notifier(MachineLane(), _runner(), _source()).background_notify)
    try:
        result = approval.check_all_command_guards("sudo systemctl restart nginx", "local")
    finally:
        approval.unregister_gateway_notify(ORIGIN)
    assert result["approved"] is False
    assert "Why:" in result["message"] and "home channel" in result["message"]


def test_an_attended_chat_is_untouched(monkeypatch):
    """A human chat keeps its own prompt, its own window, and never consults the home channel."""
    _sync_schedule(monkeypatch)
    chat, home = HumanChat(), HumanChat()
    runner = SimpleNamespace(_served_home_channel_transports=lambda: pytest.fail("home consulted"))
    notifier = _notifier(chat, runner, _source("p"))
    notifier.background_notify(dict(COMMAND))
    assert chat.cards and not home.cards
    assert notifier.wait_timeout_s() == approval_context._get_approval_timeout()


def test_rerouted_window_is_a_config_key_clamped_to_at_least_the_normal_timeout(monkeypatch):
    cfg = {}
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: cfg)
    assert approval_context._get_rerouted_approval_timeout() == 1800  # the default
    cfg.update({"timeout": 60, "rerouted_timeout": 900})
    assert approval_context._get_rerouted_approval_timeout() == 900
    cfg["rerouted_timeout"] = 5  # never shorter than the prompt a human is watching
    assert approval_context._get_rerouted_approval_timeout() == 60
    cfg["rerouted_timeout"] = "garbage"
    assert approval_context._get_rerouted_approval_timeout() == 1800


def test_timeout_notice_lands_in_the_home_channel_and_names_the_origin(monkeypatch):
    _sync_schedule(monkeypatch)
    monkeypatch.setattr(approval_context, "_get_rerouted_approval_timeout", lambda: 0.3)
    human = HumanChat()
    notify = _notifier(MachineLane(), _runner((None, human)), _source()).background_notify
    result = _await_gateway_decision(ORIGIN, notify, dict(COMMAND))
    assert result["resolved"] is False and result["choice"] is None, result  # silence is never consent
    for _ in range(100):
        if human.texts:
            break
        threading.Event().wait(0.02)
    assert human.texts, "a timeout must produce a visible notice where the prompt was shown"
    chat_id, text = human.texts[-1]
    assert chat_id == HOME_CHAT
    assert "NOT run" in text and "earshot-pm" in text and "rerouted_timeout" in text


def test_a_typed_approve_in_the_home_chat_resolves_the_origin_turn_end_to_end():
    """The real /approve handler, in a chat whose OWN session has nothing pending."""
    from gateway.approval_reroute import HumanApprovalTarget, track_rerouted
    from tests.gateway.test_approve_deny_commands import _make_event, _make_runner
    from tools.approval_gateway_wait import _ApprovalEntry

    runner = _make_runner()
    event = _make_event("/approve")  # telegram chat "c1", the home chat
    home_adapter = runner._delivery_adapter_for(event.source)
    assert home_adapter is not None
    entry = _ApprovalEntry({"command": "rm -rf /tmp/x", "request_id": "req-1"})
    approval._gateway_queues[ORIGIN] = [entry]
    track_rerouted(HumanApprovalTarget(adapter=home_adapter, chat_id="c1", platform=Platform.TELEGRAM),
                   ORIGIN, "req-1")

    reply = asyncio.run(runner._handle_approve_command(event))

    assert entry.event.is_set() and entry.result == "once", reply
    # Nothing rerouted here means the old answer: no pending approval.
    other = _make_event("/approve")
    other.source = SessionSource(platform=Platform.TELEGRAM, chat_id="elsewhere", chat_type="dm", user_id="u1")
    assert asyncio.run(runner._handle_approve_command(other)) != reply
