"""Regression: multiplex gateway profile scoping + full-prompt wiring.

Two scenarios for _agent_home's resolution order (#86313 post-merge findings):

1. MULTIPLEX INVERSION (@kshitijk4poor): the messaging gateway hands every
   agent the shared launch-home state.db but binds the profile home per turn
   via the HERMES_HOME ContextVar (copy_context into the worker). A bound
   override must WIN over the db-derived launch home, else the shared-db
   fallback stomps the correct profile deterministically.

2. BARE THREAD (the original #86313 fix): no override bound — the db-derived
   home must still win over ambient env resolution.

Plus the full-prompt wiring test (@helix4u): build_system_prompt_parts on a
bare thread with the bot's session DB must produce a prompt whose identity
(SOUL.md), skills block, and profile line ALL belong to the bot — reverting
any single call-site wire breaks this test.
"""

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class _DB:
    def __init__(self, home: Path):
        self.db_path = home / "state.db"


def _agent_for(home: Path, **overrides):
    base = dict(
        load_soul_identity=True,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        _session_db=_DB(home),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_bound_override_wins_over_shared_db_home(tmp_path, monkeypatch):
    """Multiplex lane: shared launch-home DB + per-turn ContextVar binding.
    The override must win, not the db-derived launch home."""
    from agent import system_prompt

    root = tmp_path / "root"
    root.mkdir()
    bot_home = root / "profiles" / "mybot"
    bot_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    agent = _agent_for(root)  # shared db lives at <root>/state.db
    token = set_hermes_home_override(str(bot_home))
    try:
        assert system_prompt._agent_home(agent) == bot_home
        assert (
            system_prompt._profile_name_for_home(system_prompt._agent_home(agent))
            == "mybot"
        )
    finally:
        reset_hermes_home_override(token)


def test_db_home_wins_on_bare_thread_without_override(tmp_path, monkeypatch):
    """Original #86313 scenario: unbound thread, dedicated per-profile DB."""
    from agent import system_prompt

    root = tmp_path / "root"
    bot_home = root / "profiles" / "mybot"
    bot_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    agent = _agent_for(bot_home)
    result = {}

    def resolve():
        result["home"] = system_prompt._agent_home(agent)

    t = threading.Thread(target=resolve)
    t.start()
    t.join()

    assert result["home"] == bot_home


def test_full_prompt_scoped_to_bot_on_bare_thread(tmp_path, monkeypatch):
    """Wiring test: SOUL.md identity, skills block, and profile line must ALL
    come from the bot's home when building on an unbound thread with the
    bot's session DB — no mixed-profile prompt."""
    from agent import prompt_builder
    from agent.system_prompt import build_system_prompt

    default_home = tmp_path / "root"
    default_skills = default_home / "skills" / "general" / "leaky-skill"
    default_skills.mkdir(parents=True)
    (default_skills / "SKILL.md").write_text(
        "---\nname: leaky-skill\ndescription: default-only skill\n---\nbody\n",
        encoding="utf-8",
    )
    (default_home / "SOUL.md").write_text("DEFAULT SOUL", encoding="utf-8")

    bot_home = default_home / "profiles" / "mybot"
    bot_skills = bot_home / "skills" / "general" / "bot-skill"
    bot_skills.mkdir(parents=True)
    (bot_skills / "SKILL.md").write_text(
        "---\nname: bot-skill\ndescription: bot-only skill\n---\nbody\n",
        encoding="utf-8",
    )
    (bot_home / "SOUL.md").write_text("BOT SOUL", encoding="utf-8")

    # Ambient env resolves to the launch/default home; nothing binds the
    # ContextVar on the build thread.
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=False)

    agent = _agent_for(bot_home, valid_tool_names=["skill_view"])
    result = {}

    def build():
        with (
            patch("run_agent.build_environment_hints", return_value=""),
        ):
            result["prompt"] = build_system_prompt(agent)

    t = threading.Thread(target=build)
    t.start()
    t.join()
    prompt = result["prompt"]

    assert "BOT SOUL" in prompt
    assert "DEFAULT SOUL" not in prompt
    m = re.search(r"<available_skills>(.*?)</available_skills>", prompt, re.DOTALL)
    skills_block = m.group(1) if m else ""
    assert "bot-skill" in skills_block
    assert "leaky-skill" not in skills_block
    assert "Active Hermes profile: mybot" in prompt
    assert "Active Hermes profile: default" not in prompt


def test_global_instruction_snapshot_is_profile_isolated_on_bare_thread(
    tmp_path, monkeypatch
):
    """A prompt build must use the agent's frozen config, not ambient home.

    This is the same bare-thread topology as the SOUL/skills regression above:
    the process points at the default profile while the agent belongs to a bot
    profile. Each profile names a different global file; only the bot snapshot
    may enter the bot prompt.
    """
    from agent.system_prompt import build_system_prompt

    default_home = tmp_path / "root"
    default_home.mkdir()
    default_global = tmp_path / "default-global.md"
    default_global.write_text("DEFAULT PROFILE GLOBAL", encoding="utf-8")
    (default_home / "config.yaml").write_text(
        "agent:\n"
        f"  global_instruction_files: [{default_global}]\n",
        encoding="utf-8",
    )

    bot_home = default_home / "profiles" / "mybot"
    bot_home.mkdir(parents=True)
    bot_global = tmp_path / "bot-global.md"
    bot_global.write_text("BOT PROFILE GLOBAL", encoding="utf-8")
    (bot_home / "config.yaml").write_text(
        "agent:\n"
        f"  global_instruction_files: [{bot_global}]\n",
        encoding="utf-8",
    )
    (bot_home / "SOUL.md").write_text("BOT SOUL", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))

    agent = _agent_for(
        bot_home,
        skip_context_files=False,
        global_instruction_files=(str(bot_global),),
    )
    result = {}

    def build():
        with patch("run_agent.build_environment_hints", return_value=""):
            result["prompt"] = build_system_prompt(agent)

    thread = threading.Thread(target=build)
    thread.start()
    thread.join()

    assert "BOT PROFILE GLOBAL" in result["prompt"]
    assert "DEFAULT PROFILE GLOBAL" not in result["prompt"]


def test_agent_init_captures_active_profile_global_config(tmp_path, monkeypatch):
    """Real AIAgent init snapshots the active profile's list and home."""
    from hermes_cli import config as config_mod
    from run_agent import AIAgent

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "agent:\n  global_instruction_files:\n    - fleet/AGENTS.md\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()

    with (
        patch("run_agent.OpenAI"),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            provider="meta",
            base_url="https://api.meta.ai/v1",
            api_key="test-key",
            model="muse-spark-1.2",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    try:
        assert agent.global_instruction_files == ("fleet/AGENTS.md",)
        assert agent._global_instruction_home == profile_home
    finally:
        agent.close()


def test_plugin_session_info_profile_from_agent_home(tmp_path, monkeypatch):
    """Plugin prompt metadata must carry the agent's own profile name, not the
    ambient one (@helix4u's plugin half)."""
    from agent import system_prompt

    root = tmp_path / "root"
    bot_home = root / "profiles" / "mybot"
    bot_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    agent = _agent_for(bot_home)
    result = {}

    def resolve():
        result["info"] = system_prompt._plugin_session_info(agent)

    t = threading.Thread(target=resolve)
    t.start()
    t.join()

    assert result["info"]["profile_name"] == "mybot"


def test_tilde_vs_relative_global_instruction_path_resolution(tmp_path, monkeypatch):
    """~/ paths resolve from OS home, relative paths from profile home.

    Regression for finding #4: HOME/os-home and HERMES_HOME/profile-home must
    be distinct; prove ~/... resolves from OS home while relative paths resolve
    from the owning profile home.
    """
    from hermes_cli import config as config_mod
    from run_agent import AIAgent

    # Set up OS home (distinctly different from HERMES_HOME/profile home)
    os_home = tmp_path / "os_home"
    os_home.mkdir()
    profile_home = tmp_path / "profile_home"
    profile_home.mkdir()

    # Create global files in both locations
    os_global = os_home / "os_global.md"
    os_global.write_text("OS HOME GLOBAL", encoding="utf-8")

    profile_global = profile_home / "profile_global.md"
    profile_global.write_text("PROFILE HOME GLOBAL", encoding="utf-8")

    # Exercise the real platform expansion boundary. Tilde paths use the OS
    # account home while relative paths remain anchored to the Hermes profile.
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("USERPROFILE", str(os_home))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)

    # Write config with both ~/ (OS home) and relative (profile home) paths
    (profile_home / "config.yaml").write_text(
        "agent:\n  global_instruction_files:\n    - ~/os_global.md\n    - profile_global.md\n",
        encoding="utf-8",
    )

    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()

    # Agent configured with both ~/ (OS home) and relative (profile home) paths
    with (
        patch("run_agent.OpenAI"),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            provider="meta",
            base_url="https://api.meta.ai/v1",
            api_key="test-key",
            model="test-model",
            quiet_mode=True,
            skip_memory=True,
        )

    try:
        # Verify both resolved correctly
        assert agent.global_instruction_files == ("~/os_global.md", "profile_global.md")
        assert agent._global_instruction_home == profile_home

        # Build system prompt to verify resolution
        from agent import system_prompt
        with patch("run_agent.build_environment_hints", return_value=""):
            parts = system_prompt.build_system_prompt_parts(agent)
        combined = "\n".join(parts.values())

        # Both files should be loaded with content from their respective homes
        assert "OS HOME GLOBAL" in combined, f"OS HOME GLOBAL not in:\n{combined}"
        assert "PROFILE HOME GLOBAL" in combined, f"PROFILE HOME GLOBAL not in:\n{combined}"
    finally:
        agent.close()
