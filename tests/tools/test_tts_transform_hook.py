"""``transform_tts_text`` hook: plugin rewrite of the whole spoken text before chunking.

Callbacks are registered on the real plugin manager's hook registry so the whole path
(has_hook -> bounded invoke_hook -> first-non-None-wins -> re-normalize) is exercised; only the
synthesis step is stubbed.
"""

import json
import time

import pytest

from hermes_cli.plugins import get_plugin_manager
from tools import tts_tool
from tools import tts_tool_transform as transform


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)


@pytest.fixture
def register_hook(monkeypatch):
    """Register callbacks for transform_tts_text on the active manager; restored afterwards."""
    mgr = get_plugin_manager()
    monkeypatch.setattr(mgr, "_discovered", True, raising=False)
    callbacks: list = []
    monkeypatch.setitem(mgr._hooks, transform.HOOK_NAME, callbacks)
    return callbacks


def _apply(text="Hello there.", provider="edge", cfg=None, platform=None):
    return transform.apply_tts_text_transform(text, provider, cfg or {}, platform)


class TestApplyTransform:
    def test_no_callback_is_noop_and_never_invokes(self, register_hook, monkeypatch):
        import hermes_cli.plugins as plugins
        monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **k: pytest.fail("invoked"))
        assert _apply("Hello there.") == "Hello there."

    def test_applied_when_callback_returns_text(self, register_hook):
        register_hook.append(lambda **kw: "Freakin sweet, hello there.")
        assert _apply("Hello there.") == "Freakin sweet, hello there."

    @pytest.mark.parametrize("bad", [None, "", "   \n", 42, ["x"], {"text": "x"}])
    def test_untouched_on_none_empty_or_non_str(self, register_hook, bad):
        register_hook.append(lambda **kw: bad)
        assert _apply("Hello there.") == "Hello there."

    def test_untouched_on_exception(self, register_hook):
        def boom(**kw):
            raise RuntimeError("persona exploded")
        register_hook.append(boom)
        assert _apply("Hello there.") == "Hello there."

    def test_first_non_none_wins_after_bad_results(self, register_hook):
        register_hook.extend([lambda **kw: None, lambda **kw: "  ", lambda **kw: "second",
                              lambda **kw: "third"])
        assert _apply("Hello there.") == "second"

    def test_accepted_output_is_renormalized(self, register_hook):
        register_hook.append(lambda **kw: "**Holy crap**, check `this` out!")
        out = _apply("Hello there.")
        assert "**" not in out and "`" not in out
        assert "Holy crap" in out

    def test_kwargs_contract(self, register_hook):
        seen = {}
        register_hook.append(lambda **kw: seen.update(kw))
        cfg = {"voice": "global-voice", "edge": {"voice": "edge-voice"}}
        _apply("Hello there.", provider="edge", cfg=cfg, platform="telegram")
        assert seen["text"] == "Hello there."
        assert seen["provider"] == "edge"
        assert seen["voice"] == "edge-voice"
        assert seen["platform"] == "telegram"

    def test_narrow_signature_callback_receives_only_declared_kwargs(self, register_hook):
        register_hook.append(lambda text: text.upper())
        assert _apply("hello there.") == "HELLO THERE."

    def test_async_callback_is_resolved(self, register_hook):
        async def cb(**kw):
            return "async rewrite"
        register_hook.append(cb)
        assert _apply("Hello there.") == "async rewrite"

    def test_timeout_fails_open(self, register_hook):
        register_hook.append(lambda **kw: time.sleep(2) or "too late")
        started = time.monotonic()
        out = _apply("Hello there.", cfg={"transform_timeout_seconds": 0.2})
        assert out == "Hello there."
        assert time.monotonic() - started < 1.5

    def test_slower_than_generic_hook_timeout_is_still_honored(self, register_hook, monkeypatch):
        import hermes_cli.plugins as plugins
        monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.1)
        register_hook.append(lambda **kw: time.sleep(0.4) or "slow but fine")
        assert _apply("Hello there.", cfg={"transform_timeout_seconds": 5}) == "slow but fine"

    def test_bad_timeout_config_uses_default(self):
        assert transform._resolve_timeout({"transform_timeout_seconds": "nope"}) == 45.0
        assert transform._resolve_timeout({"transform_timeout_seconds": -3}) == 45.0
        assert transform._resolve_timeout({}) == 45.0
        assert transform._resolve_timeout({"transform_timeout_seconds": 7}) == 7.0


class TestResolveVoice:
    def test_builtin_provider_section_then_top_level(self):
        assert transform.resolve_configured_voice("openai", {"openai": {"voice": "nova"}, "voice": "x"}) == "nova"
        assert transform.resolve_configured_voice("openai", {"voice": "x"}) == "x"

    def test_named_provider_blocks(self):
        assert transform.resolve_configured_voice("vox", {"providers": {"vox": {"voice": "peter"}}}) == "peter"
        assert transform.resolve_configured_voice("vox", {"vox": {"voice": "carlin"}}) == "carlin"

    def test_unknown_is_none(self):
        assert transform.resolve_configured_voice("edge", {}) is None
        assert transform.resolve_configured_voice("edge", {"edge": {"voice": "  "}}) is None


class TestTextToSpeechToolWiring:
    @pytest.fixture
    def captured(self, monkeypatch):
        monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
        monkeypatch.setattr(tts_tool, "_resolve_max_text_length", lambda p, c: 60)
        chunks_seen: list = []

        def fake_synth(chunks, *a, **kw):
            chunks_seen.append(list(chunks))
            raise tts_tool._ChunkFailed("stub: stop before audio")
        monkeypatch.setattr(tts_tool, "_synthesize_chunks", fake_synth)
        return chunks_seen

    def test_fires_once_with_whole_text_before_chunking(self, register_hook, captured):
        calls: list = []
        register_hook.append(lambda **kw: calls.append(kw["text"]))
        long_text = " ".join(f"Sentence number {i} is here." for i in range(12))
        result = json.loads(tts_tool.text_to_speech_tool(long_text))
        assert result["success"] is False  # stubbed synth
        assert len(calls) == 1
        assert calls[0] == long_text
        assert len(captured[0]) > 1  # the text really did split into several chunks

    def test_rewritten_text_is_what_gets_chunked(self, register_hook, captured):
        register_hook.append(lambda **kw: "Holy crap. " + kw["text"])
        tts_tool.text_to_speech_tool("Short and sweet.")
        assert captured[0] == ["Holy crap. Short and sweet."]

    def test_failing_callback_still_speaks_original(self, register_hook, captured):
        def boom(**kw):
            raise ValueError("nope")
        register_hook.append(boom)
        tts_tool.text_to_speech_tool("Short and sweet.")
        assert captured[0] == ["Short and sweet."]

    def test_no_hook_registered_does_not_invoke(self, register_hook, captured, monkeypatch):
        import hermes_cli.plugins as plugins
        monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **k: pytest.fail("invoked"))
        tts_tool.text_to_speech_tool("Short and sweet.")
        assert captured[0] == ["Short and sweet."]

    def test_hook_name_is_valid(self):
        from hermes_cli.plugins import VALID_HOOKS
        assert transform.HOOK_NAME in VALID_HOOKS
