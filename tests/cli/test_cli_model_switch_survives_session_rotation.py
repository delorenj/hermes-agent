"""A /model switch must not be silently undone, and its note must never lie (fork fix).

Regression for the 33GOD PM incident: ``/model automaticai/personal/glm-5.3``
was reverted to the config default by a wake-word ``new_session(silent=True)``
(``wake_word.start_new_session`` defaults True) while the stale
"[Note: model was just switched ... to glm-5.3]" was still prepended to the
next turn, so a kimi turn was told it was GLM. Four guards:

a. new_session() drops queued one-shot notes with the conversation.
b. the wake word rotates the session with ``reset_model=False``; a reset that
   does change the model is always printed, even when ``silent``.
c. chat() prepends the switch note only when the agent runs the target model.
d. an inline ``/model`` submission is appended to prompt history before
   dispatch, so the picker's buffer snapshot cannot swallow it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.cli.test_cli_new_session import _FakeAgent, _make_cli


CONFIG_DEFAULT = "anthropic/claude-opus-4.6"
SWITCHED = "automaticai/personal/glm-5.3"


@pytest.fixture(autouse=True)
def _reset_session_id_context():
    import os

    from gateway.session_context import _UNSET, _VAR_MAP

    yield
    os.environ.pop("HERMES_SESSION_ID", None)
    _VAR_MAP["HERMES_SESSION_ID"].set(_UNSET)


def _switched_cli(capture):
    """A CLI that has just applied ``/model SWITCHED`` (note queued)."""
    cli = _make_cli()
    cli._session_db = None
    cli.agent = _FakeAgent("sess", datetime.now())
    cli.agent.switch_model = MagicMock()
    cli.model = SWITCHED
    cli.provider = "custom"
    cli.agent.model = SWITCHED
    cli._pending_model_switch_note = f"[Note: model was just switched from x to {SWITCHED}.]"
    cli._pending_model_switch_target = SWITCHED
    cli._pending_skills_reload_note = "[Note: skills reloaded]"
    cli._pending_one_turn_model_restore = {"model": CONFIG_DEFAULT}
    capture.arm()
    return cli


def _reset_result():
    return SimpleNamespace(
        success=True,
        new_model=CONFIG_DEFAULT,
        target_provider="openrouter",
        api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
    )


@pytest.fixture
def printed(monkeypatch):
    """Capture _cprint and pin config.yaml's model.default for new_session().

    ``_make_cli`` reloads the ``cli`` module, which would undo a patch made
    before it runs, so the patch is (re)applied lazily via ``printed.arm()``.
    """
    lines = _Lines()

    def arm():
        import cli as cli_mod

        monkeypatch.setattr(cli_mod, "_cprint", lambda s, *a, **k: lines.append(str(s)))
        config = dict(cli_mod.CLI_CONFIG)
        config["model"] = {"default": CONFIG_DEFAULT, "provider": "openrouter"}
        config.setdefault("agent", {})
        monkeypatch.setattr(cli_mod, "CLI_CONFIG", config)

    lines.arm = arm
    return lines


class _Lines(list):
    arm = None


def test_new_session_drops_queued_notes(printed):
    cli = _switched_cli(printed)
    with patch("hermes_cli.model_switch.switch_model", return_value=_reset_result()):
        cli.new_session(silent=True)
    assert cli._pending_model_switch_note is None
    assert cli._pending_model_switch_target is None
    assert cli._pending_skills_reload_note is None
    assert cli._pending_one_turn_model_restore is None


def test_silent_new_session_still_announces_a_model_reset(printed):
    cli = _switched_cli(printed)
    with patch("hermes_cli.model_switch.switch_model", return_value=_reset_result()):
        notice = cli.new_session(silent=True)
    assert cli.model == CONFIG_DEFAULT
    assert any(f"(model reset to config default: {CONFIG_DEFAULT})" in ln for ln in printed)
    assert notice and CONFIG_DEFAULT in notice


def test_reset_model_false_keeps_the_running_model(printed):
    cli = _switched_cli(printed)
    with patch("hermes_cli.model_switch.switch_model") as sm:
        notice = cli.new_session(silent=True, reset_model=False)
    sm.assert_not_called()
    cli.agent.switch_model.assert_not_called()
    assert cli.model == SWITCHED
    assert cli.provider == "custom"
    assert notice is None
    assert not any("model reset" in ln for ln in printed)
    # The session still rotated and the stale note went with it, but a
    # pending one-turn restore survives with the model it belongs to.
    assert cli._pending_model_switch_note is None
    assert cli._pending_one_turn_model_restore == {"model": CONFIG_DEFAULT}


def test_reset_model_is_keyword_only():
    import inspect

    import cli as cli_mod

    param = inspect.signature(cli_mod.HermesCLI.new_session).parameters["reset_model"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is True


def test_wake_word_rotates_without_resetting_the_model(printed):
    cli = _switched_cli(printed)
    cli._agent_running = False
    cli._voice_recording = False
    cli._voice_processing = False
    cli._wake_start_new_session = True
    cli._app = None
    cli.new_session = MagicMock()
    cli._voice_start_recording = MagicMock()
    with patch("tools.wake_word.pause_listening", return_value=True), patch(
        "tools.wake_word.get_last_match", return_value=None
    ):
        cli._on_wake_word()
    cli.new_session.assert_called_once_with(silent=True, reset_model=False)


def test_clear_reprints_the_reset_notice_after_wiping_the_screen(printed, tmp_path):
    from tests.cli.test_cli_new_session import _prepare_cli_with_active_session

    cli = _prepare_cli_with_active_session(tmp_path)
    printed.arm()
    cli.console = MagicMock()
    cli.show_banner = MagicMock()
    cli.agent.switch_model = MagicMock()
    cli.model = SWITCHED
    with patch("hermes_cli.model_switch.switch_model", return_value=_reset_result()), \
         patch("builtins.print") as fake_print:
        cli.process_command("/clear")
    shown = [str(c.args[0]) for c in fake_print.call_args_list if c.args]
    assert any(f"model reset to config default: {CONFIG_DEFAULT}" in s for s in shown), shown


def test_switch_note_is_prepended_when_agent_runs_the_target():
    cli = _make_cli()
    cli.agent = SimpleNamespace(model=SWITCHED, provider="custom")
    cli._pending_model_switch_note = "[NOTE]"
    cli._pending_model_switch_target = SWITCHED
    assert cli._take_pending_model_switch_note() == "[NOTE]"
    assert cli._pending_model_switch_note is None
    assert cli._pending_model_switch_target is None


def test_switch_note_is_dropped_and_logged_when_switch_did_not_survive(caplog):
    cli = _make_cli()
    cli.agent = SimpleNamespace(model="kimi-for-coding", provider="kimi-coding")
    cli._pending_model_switch_note = "[NOTE]"
    cli._pending_model_switch_target = SWITCHED
    with caplog.at_level(logging.WARNING):
        assert cli._take_pending_model_switch_note() is None
    assert cli._pending_model_switch_note is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        SWITCHED in r.getMessage() and "did not survive" in r.getMessage() for r in warnings
    )


def test_untargeted_switch_note_is_still_prepended():
    """Older callers queue a note without a target; keep their behaviour."""
    cli = _make_cli()
    cli.agent = SimpleNamespace(model="whatever", provider="x")
    cli._pending_model_switch_note = "[NOTE]"
    cli._pending_model_switch_target = None
    assert cli._take_pending_model_switch_note() == "[NOTE]"


def test_apply_model_switch_records_the_target(printed):
    cli = _make_cli()
    printed.arm()
    cli.agent = None
    result = SimpleNamespace(
        success=True,
        new_model=SWITCHED,
        target_provider="custom",
        provider_label="AutomaticAI",
        api_key="sk-dummy",
        base_url="https://api.automaticai.io/v1",
        api_mode="chat_completions",
        model_info=None,
        warning_message="",
        is_global=False,
    )
    try:
        cli._apply_model_switch_result(result, persist_global=False)
    except Exception:
        pass  # later display steps may need more state; the note is set first
    assert cli._pending_model_switch_target == SWITCHED
    assert SWITCHED.split("/")[-1] in (cli._pending_model_switch_note or "")


class _History:
    def __init__(self):
        self.strings: list[str] = []

    def get_strings(self):
        return list(self.strings)

    def append_string(self, s):
        self.strings.append(s)


class _Buffer:
    """Mirrors prompt_toolkit Buffer.append_to_history's de-duplication."""

    def __init__(self, text):
        self.text = text
        self.history = _History()

    def append_to_history(self):
        if self.text:
            hs = self.history.get_strings()
            if not hs or hs[-1] != self.text:
                self.history.append_string(self.text)

    def reset(self, append_to_history=False):
        if append_to_history:
            self.append_to_history()
        self.text = ""


def test_inline_model_command_is_recorded_before_the_picker_clears_the_buffer():
    cli = _make_cli()
    buf = _Buffer("/model")
    seen_at_dispatch = []

    def _process(text):
        seen_at_dispatch.append(list(buf.history.strings))
        buf.reset()  # what _capture_modal_input_snapshot does when the picker opens
        return True

    cli.process_command = _process
    assert cli._run_inline_model_command(buf, "/model") is True
    buf.reset(append_to_history=True)  # handle_enter's post-dispatch reset
    assert seen_at_dispatch == [["/model"]]
    assert buf.history.strings == ["/model"]


def test_inline_model_command_with_args_is_not_recorded_twice():
    cli = _make_cli()
    buf = _Buffer(f"/model {SWITCHED}")
    cli.process_command = lambda text: True
    cli._run_inline_model_command(buf, buf.text)
    buf.reset(append_to_history=True)
    assert buf.history.strings == [f"/model {SWITCHED}"]
