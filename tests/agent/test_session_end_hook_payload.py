"""``on_session_end`` carries the finalized reply and the session cwd.

The finalizer had ``final_response`` in scope but omitted it, so a turn-completion hook
(artifact sidecar, CI notify, usage sinks) learned THAT a turn ended but never WHAT it
answered — the one message a completion signal needs. The session's cwd rides along the
same way so a hook can attribute the turn to a project without scraping the transcript.
"""

from unittest.mock import patch

import pytest

# Settle the lazy import chain finalize_turn performs (conversation_loop → process_bootstrap →
# hermes_bootstrap's update-recovery stat) at COLLECTION time. Under a git-worktree checkout the
# marker stat resolves inside the real Hermes home, which the per-test home_io_guard refuses —
# importing here runs it once, unguarded, before any test body executes.
import agent.conversation_loop  # noqa: F401

from agent.turn_finalizer import finalize_turn
from tests.agent.test_turn_finalizer_cleanup_guard import _StubAgent, _run


def _capture_hooks(agent, **run_kwargs):
    """Run a clean finalize_turn and return every ``_invoke_hook_safely`` call as
    ``(name, kwargs)`` in firing order."""
    calls = []

    def capture(name, _logger, **kwargs):
        calls.append((name, kwargs))
        return []

    with patch("agent.turn_finalizer._invoke_hook_safely", side_effect=capture):
        _run(agent, **run_kwargs)
    return calls


class TestSessionEndHookPayload:
    def test_completed_turn_delivers_final_response_and_cwd(self):
        agent = _StubAgent(raise_in=())
        agent.session_cwd = "/home/user/project"
        calls = _capture_hooks(
            agent,
            final_response="Report written to artifacts/report.md",
            api_call_count=1,
            turn_exit_reason="text_response(stop)",
        )
        end = next(kw for name, kw in calls if name == "on_session_end")
        assert end["final_response"] == "Report written to artifacts/report.md"
        assert end["cwd"] == "/home/user/project"
        assert end["completed"] is True
        assert end["turn_exit_reason"] == "text_response(stop)"

    def test_interrupted_turn_final_response_is_empty_not_invented(self):
        """An interrupted turn sends the real (empty) reply; nothing is synthesized to
        make the payload look complete."""
        agent = _StubAgent(raise_in=())
        calls = []
        with patch(
            "agent.turn_finalizer._invoke_hook_safely",
            side_effect=lambda name, _logger, **kw: (calls.append((name, kw)), [])[1],
        ):
            from agent.turn_finalizer import finalize_turn

            finalize_turn(
                agent,
                final_response="",
                api_call_count=1,
                interrupted=True,
                failed=False,
                messages=[{"role": "user", "content": "hi"}],
                conversation_history=None,
                effective_task_id="task-1",
                turn_id="turn-1",
                user_message="hi",
                original_user_message="hi",
                _should_review_memory=False,
                _turn_exit_reason="interrupted_during_api_call",
            )
        end = next(kw for name, kw in calls if name == "on_session_end")
        assert end["final_response"] == ""
        assert end["interrupted"] is True

    def test_no_session_cwd_attribute_sends_none_not_process_cwd(self):
        """A stub/legacy agent without ``session_cwd`` sends ``None`` — the shell-hook
        bridge then resolves per-session state; the process cwd never leaks in here."""
        agent = _StubAgent(raise_in=())
        assert not hasattr(agent, "session_cwd")
        calls = _capture_hooks(agent, final_response="done", api_call_count=1,
                               turn_exit_reason="text_response(stop)")
        end = next(kw for name, kw in calls if name == "on_session_end")
        assert end["cwd"] is None

    def test_persist_disabled_turn_still_omits_on_session_end(self):
        """Detached forks keep the existing no-publish contract (unchanged by this fix)."""
        agent = _StubAgent(raise_in=())
        agent._persist_disabled = True
        calls = _capture_hooks(agent, final_response="done", api_call_count=1,
                               turn_exit_reason="text_response(stop)")
        assert all(name != "on_session_end" for name, _ in calls)

    def test_shared_metrics_task_terminal_fields_ignore_the_new_kwargs(self):
        """Privacy contract: the shared-metrics terminal payload stays bounded —
        ``final_response``/``cwd`` flow to hooks, never into metrics fields."""
        from hermes_cli.observability.shared_metrics_contract import task_terminal_fields

        fields = task_terminal_fields(
            {"platform": "cli", "session_id": "s", "turn_exit_reason": "text_response(stop)",
             "completed": True, "interrupted": False, "final_response": "SECRET REPLY TEXT", "cwd": "/private/path"},
            duration_ms=1000, model_call_count=1, tool_call_count=0, retry_count=0,
        )
        assert "SECRET REPLY TEXT" not in fields.values()
        assert "/private/path" not in fields.values()
        assert fields["outcome"] == "success"


@pytest.mark.parametrize(
    ("interrupted", "failed", "final_response"),
    [
        (False, False, "ok"),
        (True, False, ""),
        (False, True, "error text"),
    ],
)
def test_on_session_end_always_fires_with_the_real_outcome(interrupted, failed, final_response):
    agent = _StubAgent(raise_in=())
    calls = []
    with patch(
        "agent.turn_finalizer._invoke_hook_safely",
        side_effect=lambda name, _logger, **kw: (calls.append((name, kw)), [])[1],
    ):
        from agent.turn_finalizer import finalize_turn

        finalize_turn(
            agent,
            final_response=final_response,
            api_call_count=1,
            interrupted=interrupted,
            failed=failed,
            messages=[{"role": "user", "content": "hi"}],
            conversation_history=None,
            effective_task_id="task-1",
            turn_id="turn-1",
            user_message="hi",
            original_user_message="hi",
            _should_review_memory=False,
            _turn_exit_reason="interrupted_during_api_call" if interrupted else "text_response(stop)",
        )
    end = next(kw for name, kw in calls if name == "on_session_end")
    assert end["final_response"] == final_response
    assert end["interrupted"] is interrupted
    assert end["failed"] is failed
