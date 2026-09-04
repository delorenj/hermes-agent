"""The novelty gate that stops cron agents retaining their own prompt.

Measured on 2026-09-04: the ``agent-33god-pm`` bank was 98.4% of all
Hindsight LLM spend (6,918,531 tokens in a day against 115,899 for the next
bank). Each cron tick retained 80,153 characters of which 66,805 were six
skill documents the harness inlines and 12,264 were the cron instruction,
both byte-identical every tick; only the assistant's 1,084-character status
block ever changed. 110 of the 116 facts extracted from one tick restated
the skill documentation.

These tests pin the three layers that fix it and, above all, that the gate
fails OPEN — losing a memory is worse than paying for a duplicate one.
"""

import importlib
import json

import pytest

gate_mod = importlib.import_module("plugins.memory.hindsight.retain_gate")
RetainGate = gate_mod.RetainGate
strip_injected_blocks = gate_mod.strip_injected_blocks


SKILL_BLOCK = (
    '[IMPORTANT: The user has invoked the "momo" skill, indicating they want you '
    "to follow its instructions. The full skill content is loaded below.]\n\n"
    "---\nname: momo\ndescription: Momo is a project manager.\n---\n\n"
    "Momo delegates every code change to a subagent and never edits code itself.\n"
)

# The real cron block quotes "[SILENT]" inside itself — twice. A non-greedy
# regex ends the marker at the quoted bracket and leaks the rest of the block.
CRON_BLOCK = (
    "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: your reply is "
    'delivered automatically. SILENT: if there is nothing to report, respond with '
    'exactly "[SILENT]" (nothing else). Never combine [SILENT] with content.]'
)

REAL_WORK = (
    "STATUS: BLOCKED\n"
    "TICKET: 33GOD-53\n"
    "ACTION: the availability probe returned pre_execution_quota_exhausted, so the "
    "review range and the sole work-in-progress lease were held without consuming a "
    "verdict or dispatching duplicate work anywhere in the campaign.\n"
    "NEXT: reverify the worker and the canonical handback after cooldown."
)


def tick(work: str = REAL_WORK) -> str:
    """A turn shaped like a real cron tick: scaffolding, prompt, then work."""
    return (
        f"User: {SKILL_BLOCK}\n{CRON_BLOCK}\n\n"
        "You are the five-minute implementation controller for this project.\n\n"
        "Five-minute discipline:\n- One implementation item in progress.\n"
        "- Never repeat a side effect without checking idempotency first.\n\n"
        f"Assistant: {work}"
    )


class TestStripInjectedBlocks:
    def test_a_loaded_skill_takes_its_body_with_it(self):
        out = strip_injected_blocks(f"before\n{SKILL_BLOCK}\n{CRON_BLOCK}\nafter")
        assert "Momo delegates every code change" not in out
        assert "name: momo" not in out
        assert "before" in out and "after" in out

    def test_an_unbounded_skill_body_keeps_the_text_after_it(self):
        # No block follows, so the remaining text is the user's real message.
        # Dropping it would be the one unrecoverable mistake here.
        out = strip_injected_blocks(f"{SKILL_BLOCK}\nmy actual question")
        assert "my actual question" in out
        assert "The full skill content is loaded below" not in out

    def test_nested_brackets_do_not_end_the_block_early(self):
        # The bug this pins: ending at the quoted "[SILENT]" leaked the tail.
        out = strip_injected_blocks(f"{CRON_BLOCK}\n\nthe real prompt")
        assert out.strip() == "the real prompt"
        assert "suppress" not in out and "DELIVERY" not in out

    def test_scaffolding_that_loads_no_skill_keeps_the_prose_after_it(self):
        out = strip_injected_blocks("[IMPORTANT: be brief.]\n\nkeep me")
        assert "keep me" in out and "be brief" not in out

    def test_consecutive_skill_blocks_collapse(self):
        out = strip_injected_blocks(SKILL_BLOCK + SKILL_BLOCK + CRON_BLOCK + "\n\ntail")
        assert out.strip() == "tail"

    def test_unterminated_block_runs_to_the_end(self):
        assert strip_injected_blocks("head [IMPORTANT: oops").strip() == "head"

    def test_text_with_no_blocks_is_untouched(self):
        assert strip_injected_blocks("plain text") == "plain text"

    def test_empty_input(self):
        assert strip_injected_blocks("") == ""

    def test_it_removes_the_bulk_of_a_real_shaped_tick(self):
        raw = tick()
        assert len(strip_injected_blocks(raw)) < len(raw) * 0.75


class TestParagraphKeys:
    def test_clocks_do_not_change_the_key(self):
        key = gate_mod.paragraph_key
        assert key("ran at 2026-09-04T06:21:48Z") == key("ran at 2026-09-01T01:02:03Z")
        assert key("probe-20260904T062148Z.json") == key("probe-20260101T000000Z.json")
        assert key("refreshed at 06:21:48") == key("refreshed at 23:59:01")
        assert key("a  b\n c") == key("a b c")

    def test_content_that_is_not_a_clock_does_change_the_key(self):
        # The bug this pins: folding away integers and hex made a move from
        # one ticket to the next look like a repeated heartbeat, so a real
        # state transition was dropped as a duplicate.
        key = gate_mod.paragraph_key
        assert key("TICKET: 33GOD-53") != key("TICKET: 33GOD-54")
        assert key("closed 6 tickets") != key("closed 91 tickets")
        assert key("worktree at cc00fe6ef855e506") != key("worktree at 467e3d4b069befd0")
        assert key("the build passed") != key("the build failed")

    def test_split_and_filter_round_trip(self):
        text = "one\n\ntwo\n\nthree"
        assert gate_mod.split_paragraphs(text) == ["one", "two", "three"]
        dropped = {gate_mod.paragraph_key("two")}
        assert gate_mod.filter_paragraphs(text, dropped) == "one\n\nthree"

    def test_filter_with_nothing_dropped_is_identity(self):
        assert gate_mod.filter_paragraphs("a\n\nb", set()) == "a\n\nb"


class TestGate:
    def test_the_first_tick_is_always_retained(self, tmp_path):
        d = RetainGate("bank", state_dir=tmp_path).decide(tick())
        assert d.retain and d.reason == "novel"

    def test_repeated_boilerplate_is_dropped_once_it_repeats(self, tmp_path):
        g = RetainGate("bank", state_dir=tmp_path, threshold=3)
        for i in range(3):
            g.decide(tick(f"STATUS: RUNNING\nACTION: step {i} of the campaign was "
                          f"dispatched to a worker and the lease was refreshed."))
        d = g.decide(tick())
        assert d.retain
        assert "five-minute implementation controller" not in d.text
        assert "STATUS: BLOCKED" in d.text
        assert d.kept_chars < d.original_chars * 0.45

    def test_an_unchanged_tick_is_not_retained_at_all(self, tmp_path):
        # threshold high enough that layer 2 stays out of the way, so this
        # pins the unchanged-residue check on its own.
        g = RetainGate("bank", state_dir=tmp_path, threshold=99)
        g.decide(tick())
        d = g.decide(tick())
        assert not d.retain
        assert d.reason == "identical to the previous retain"

    def test_a_real_transition_still_gets_through(self, tmp_path):
        g = RetainGate("bank", state_dir=tmp_path, threshold=1)
        g.decide(tick())
        g.decide(tick())
        moved = tick("STATUS: INTEGRATED\nTICKET: 33GOD-54\nACTION: the reviewer "
                     "returned a pass and the branch was merged into the main line "
                     "after the full suite ran green against the live service.")
        d = g.decide(moved)
        assert d.retain and "INTEGRATED" in d.text

    def test_a_short_transition_clears_the_floor(self, tmp_path):
        # The floor exists to refuse scraps, not real one-line transitions.
        g = RetainGate("bank", state_dir=tmp_path, threshold=1)
        g.decide(tick())
        d = g.decide(tick("STATUS: INTEGRATED. Ticket 33GOD-54 merged into the "
                          "main line after the suite ran green."))
        assert d.retain, d.reason

    def test_the_silent_marker_is_never_retained(self, tmp_path):
        d = RetainGate("bank", state_dir=tmp_path).decide("Assistant: [SILENT]")
        assert not d.retain and "SILENT" in d.reason

    def test_a_thin_residue_is_not_worth_an_llm_call(self, tmp_path):
        d = RetainGate("bank", state_dir=tmp_path, min_novel_chars=200).decide("ok")
        assert not d.retain and "floor" in d.reason

    def test_state_survives_a_new_process(self, tmp_path):
        for _ in range(3):
            RetainGate("bank", state_dir=tmp_path, threshold=3).decide(tick())
        fresh = RetainGate("bank", state_dir=tmp_path, threshold=3)
        d = fresh.evaluate(tick("STATUS: DISPATCHED\nACTION: a new worker was given "
                                "the ticket and the lease was taken for this tick."))
        assert d.dropped_keys, "a new process must see the rolling window on disk"

    def test_banks_do_not_share_a_window(self, tmp_path):
        RetainGate("one", state_dir=tmp_path, threshold=1).decide(tick())
        d = RetainGate("two", state_dir=tmp_path, threshold=1).decide(tick())
        assert d.retain

    def test_a_bank_name_cannot_escape_the_state_dir(self, tmp_path):
        g = RetainGate("../../etc/passwd", state_dir=tmp_path)
        assert g.state_path.parent == tmp_path

    def test_the_window_stays_bounded(self, tmp_path):
        g = RetainGate("bank", state_dir=tmp_path, window=5)
        for i in range(20):
            g.decide(tick(f"STATUS: RUNNING\nACTION: tick {i} did a thing that was "
                          f"long enough to clear the novelty floor comfortably."))
        assert len(json.loads(g.state_path.read_text())["windows"]) == 5

    def test_every_layer_can_be_turned_off(self, tmp_path):
        g = RetainGate("bank", state_dir=tmp_path, strip_injected=False,
                       drop_repeated=False, skip_unchanged=False, min_novel_chars=0)
        raw = tick()
        g.decide(raw)
        d = g.decide(raw)
        assert d.retain and d.kept_chars == len(raw)


class TestFailsOpen:
    def test_corrupt_state_is_ignored_not_fatal(self, tmp_path):
        g = RetainGate("bank", state_dir=tmp_path)
        g.state_path.parent.mkdir(parents=True, exist_ok=True)
        g.state_path.write_text("{not json")
        assert g.decide(tick()).retain

    def test_an_unwritable_state_dir_still_retains(self, tmp_path):
        blocked = tmp_path / "wall"
        blocked.write_text("i am a file, not a directory")
        d = RetainGate("bank", state_dir=blocked / "under").decide(tick())
        assert d.retain

    def test_an_internal_error_fails_open(self, tmp_path, monkeypatch):
        g = RetainGate("bank", state_dir=tmp_path)
        monkeypatch.setattr(gate_mod, "strip_injected_blocks",
                            lambda _t: (_ for _ in ()).throw(RuntimeError("boom")))
        d = g.decide(tick())
        assert d.retain and "failing open" in d.reason
        assert d.text == tick(), "failing open must not alter the payload"

    def test_empty_and_none_are_safe(self, tmp_path):
        g = RetainGate("bank", state_dir=tmp_path)
        assert not g.decide("").retain
        assert not g.decide(None).retain


class TestTheMeasuredCase:
    """The shape that cost 6.9M tokens a day, end to end."""

    def test_a_run_of_ticks_collapses_to_the_status_block(self, tmp_path):
        g = RetainGate("agent-33god-pm", state_dir=tmp_path, threshold=2)
        sizes = []
        for i in range(6):
            work = REAL_WORK if i % 2 else REAL_WORK.replace("BLOCKED", "RUNNING")
            d = g.decide(tick(work))
            sizes.append(d.kept_chars if d.retain else 0)
        # The steady state keeps only the work, not the prompt around it.
        assert sizes[-1] < len(tick()) * 0.1
        assert sum(1 for s in sizes if s == 0) >= 1, "no-op ticks must be skipped"


class TestTheGateIsActuallyReached:
    """A gate nothing calls saves nothing.

    These assert the call site in ``sync_turn``, not just the gate's own
    behaviour: the measured failure was never that the logic was wrong, it
    was that nothing stood between a cron tick and the extractor.
    """

    @staticmethod
    def _provider(monkeypatch, tmp_path, **config):
        hindsight = importlib.import_module("plugins.memory.hindsight")
        monkeypatch.setattr(hindsight, "_load_config", lambda: {"mode": "disabled"})
        p = hindsight.HindsightMemoryProvider()
        p._bank_id = "agent-33god-pm"
        p._auto_retain = True
        p._retain_every_n_turns = 1
        p._retain_gate_enabled = config.get("gate", True)
        p._retain_strip_injected = config.get("strip", True)
        p._retain_drop_repeated = config.get("dedupe", True)
        p._retain_skip_unchanged = config.get("skip_unchanged", True)
        p._retain_dedupe_window = config.get("window", 20)
        p._retain_dedupe_threshold = config.get("threshold", 2)
        p._retain_min_novel_chars = config.get("floor", 64)

        queued: list = []
        monkeypatch.setattr(p, "_ensure_writer", lambda: None)
        monkeypatch.setattr(p, "_register_atexit", lambda: None)
        monkeypatch.setattr(p, "_emit_saving_indicator", lambda: None)
        monkeypatch.setattr(p, "_resolve_retain_target", lambda _d: ("doc", None))
        monkeypatch.setattr(p, "_retain_queue", type("Q", (), {"put": lambda _s, fn: queued.append(fn)})())
        # Point the gate's rolling window at the test's own directory.
        real_gate = gate_mod.RetainGate
        monkeypatch.setattr(
            gate_mod, "RetainGate",
            lambda bank, **kw: real_gate(bank, state_dir=tmp_path, **kw),
        )
        return p, queued

    def test_an_unchanged_tick_never_reaches_the_writer(self, monkeypatch, tmp_path):
        p, queued = self._provider(monkeypatch, tmp_path, dedupe=False)
        user, assistant = tick().split("Assistant: ")
        p.sync_turn(user, assistant, session_id="cron_1")
        assert len(queued) == 1, "the first tick must be retained"
        p._session_turns.clear()
        p._turn_counter = 0
        p.sync_turn(user, assistant, session_id="cron_2")
        assert len(queued) == 1, "the identical second tick must be refused"

    def test_the_stored_payload_loses_the_skill_documentation(self, monkeypatch, tmp_path):
        p, queued = self._provider(monkeypatch, tmp_path)
        user, assistant = tick().split("Assistant: ")
        p.sync_turn(user, assistant, session_id="cron_1")
        stored = json.dumps(p._session_turns)
        assert "Momo delegates every code change" not in stored
        assert "STATUS: BLOCKED" in stored

    def test_a_novel_tick_still_reaches_the_writer(self, monkeypatch, tmp_path):
        p, queued = self._provider(monkeypatch, tmp_path)
        for i in range(4):
            p._session_turns.clear()
            p._turn_counter = 0
            user, assistant = tick(
                f"STATUS: RUNNING\nTICKET: 33GOD-{i}\nACTION: dispatched worker "
                f"number {i} to the ticket and refreshed the lease for this tick."
            ).split("Assistant: ")
            p.sync_turn(user, assistant, session_id=f"cron_{i}")
        assert len(queued) == 4, "every genuinely new tick must be retained"

    def test_turning_the_gate_off_restores_the_old_behaviour(self, monkeypatch, tmp_path):
        p, queued = self._provider(monkeypatch, tmp_path, gate=False)
        user, assistant = tick().split("Assistant: ")
        for i in range(3):
            p._session_turns.clear()
            p._turn_counter = 0
            p.sync_turn(user, assistant, session_id=f"cron_{i}")
        assert len(queued) == 3
        assert "Momo delegates every code change" in json.dumps(p._session_turns)

    def test_a_broken_gate_does_not_lose_the_retain(self, monkeypatch, tmp_path):
        p, queued = self._provider(monkeypatch, tmp_path)
        monkeypatch.setattr(
            gate_mod, "RetainGate",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gate exploded")),
        )
        user, assistant = tick().split("Assistant: ")
        p.sync_turn(user, assistant, session_id="cron_1")
        assert len(queued) == 1, "a broken gate must fail open, not swallow the turn"
