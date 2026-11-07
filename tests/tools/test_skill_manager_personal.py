"""Personal source checkpoint regression contracts (SKRILL-47).

Real skill_manage registry dispatch and filesystem writes; only the external
Skillex executable is a temporary protocol fixture, never the live bridge.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import skill_manager_personal as personal
from tools import skill_manager_tool as manager
from tools.registry import registry


def content(name="learned", body="Run the verified procedure."):
    return f"---\nname: {name}\ndescription: Use when testing a learned procedure.\n---\n\n# Procedure\n\n{body}\n"


@pytest.fixture
def desk(tmp_path, monkeypatch):
    account = tmp_path / "account"
    profile = account / ".hermes/profiles/alice"
    source = account / ".agents/workforce/alice/learned-skills"
    shared = account / "shared"
    for path in (profile / "skills", source, shared):
        path.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: account)
    monkeypatch.setenv("HOME", str(account))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(manager, "SKILLS_DIR", profile / "skills")
    monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [profile / "skills", shared])
    monkeypatch.setattr("agent.skill_utils.get_skill_search_roots", lambda: [(0, shared), (1, profile / "skills")])
    monkeypatch.setattr("agent.skill_utils.get_skill_create_dir", lambda: shared)
    monkeypatch.setattr("tools.environments.local.served_profile_child_env",
                        lambda **kw: {"HOME": str(account), "HERMES_HOME": str(kw["target_home"])})
    monkeypatch.setattr("tools.write_approval.evaluate_gate",
                        lambda *a: type("Gate", (), {"allow": True})())
    marker = profile / ".skillex-personal-source.json"
    marker.write_text(json.dumps({
        "schema": 1, "identity": "alice", "source": str(source),
        "repository": str(account / ".agents"),
        "definition": str(source.parent / "agent.yaml"),
    }))
    bridge = account / ".local/bin/skillex-personal-checkpoint"
    bridge.parent.mkdir(parents=True)
    bridge.write_text(f"#!{sys.executable} -I\n" + '''import json, os, sys
from pathlib import Path
command = sys.argv[1]
profile = Path(sys.argv[sys.argv.index("--profile") + 1])
with (profile / ".bridge-log").open("a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "home": os.getenv("HERMES_HOME")}) + "\\n")
mode_file = profile / ".bridge-mode"
mode = mode_file.read_text() if mode_file.exists() else ""
if mode == "invalid-json":
    print("not JSON")
    sys.exit(0)
failed = mode == command
print(json.dumps({"ok": not failed, "commit": "a" * 40 if command == "checkpoint" else None,
                  "pushed": command == "checkpoint" and not failed,
                  "error": "push rejected" if failed else None}))
sys.exit(1 if failed else 0)
''')
    bridge.chmod(0o755)
    return profile, source, shared, bridge


def invoke(**args):
    return json.loads(registry.dispatch("skill_manage", args))


def calls(profile):
    log = profile / ".bridge-log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def seed(root, name="learned", advertised=None):
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(content(advertised or name))
    return path


def test_create_goes_to_bound_source_not_configured_shared_root(desk):
    profile, source, shared, _ = desk
    result = invoke(action="create", name="learned", content=content())
    assert result["success"], result
    assert (source / "learned/SKILL.md").read_text() == content()
    assert not (shared / "learned").exists()
    assert result["checkpoint"]["pushed"] is True
    logged = calls(profile)
    assert logged[0]["argv"] == ["check", "--profile", str(profile)]
    assert logged[-1]["argv"] == ["checkpoint", "--profile", str(profile)]
    assert all(row["home"] == str(profile) for row in logged)


@pytest.mark.parametrize("action,extra", [
    ("edit", {"content": content(body="Updated procedure.")}),
    ("patch", {"old_string": "verified procedure", "new_string": "improved procedure"}),
    ("write_file", {"file_path": "references/guide.md", "file_content": "Support."}),
    ("remove_file", {"file_path": "references/guide.md"}),
    ("delete", {}),
])
def test_every_mutation_checkpoints(desk, action, extra):
    profile, source, _, _ = desk
    skill = seed(source)
    (skill / "references").mkdir()
    (skill / "references/guide.md").write_text("Original.")
    result = invoke(action=action, name="learned", **extra)
    assert result["success"], result
    assert result["checkpoint"]["pushed"] is True
    assert [row["argv"][0] for row in calls(profile)].count("checkpoint") == 1
    if action == "delete":
        assert not skill.exists()
    elif action == "remove_file":
        assert not (skill / "references/guide.md").exists()
    elif action == "write_file":
        assert (skill / "references/guide.md").read_text() == "Support."
    else:
        assert (skill / "SKILL.md").read_text() != content()


def test_categorized_personal_skill_resolves_and_delete_preserves_source_root(desk):
    _, source, _, _ = desk
    assert invoke(action="create", name="learned", category="testing", content=content())["success"]
    result = invoke(action="patch", name="testing/learned", old_string="verified", new_string="improved")
    assert result["success"], result
    assert invoke(action="delete", name="testing/learned")["success"]
    assert source.is_dir()
    assert invoke(action="create", name="learned", content=content())["success"]
    assert invoke(action="delete", name="learned")["success"]
    assert source.is_dir()


def test_batch_commits_once_after_all_writes_and_retains_on_push_failure(desk):
    profile, source, _, _ = desk
    (profile / ".bridge-mode").write_text("checkpoint")
    result = invoke(operations=[
        {"action": "create", "name": "learned", "content": content()},
        {"action": "write_file", "name": "learned", "file_path": "scripts/run.py", "file_content": "print(1)"},
    ])
    assert result["success"] is False
    assert result["content_retained"] is True
    assert "Durable personal skill checkpoint failed" in result["error"]
    assert (source / "learned/SKILL.md").exists()
    assert (source / "learned/scripts/run.py").read_text() == "print(1)"
    assert [row["argv"][0] for row in calls(profile)].count("checkpoint") == 1


def test_failed_batch_rolls_back_without_checkpoint(desk):
    profile, source, _, _ = desk
    result = invoke(operations=[
        {"action": "create", "name": "learned", "content": content()},
        {"action": "patch", "name": "learned", "old_string": "missing", "new_string": "new"},
    ])
    assert not result["success"]
    assert not (source / "learned").exists()
    assert not any(row["argv"][0] == "checkpoint" for row in calls(profile))


def test_shared_selection_is_never_mutated_as_personal(desk):
    profile, _, shared, _ = desk
    skill = seed(shared, "shared")
    result = invoke(action="edit", name="shared", content=content("shared", "Overwrite."))
    assert not result["success"]
    assert "selected shared/project skill" in result["error"]
    assert (skill / "SKILL.md").read_text() == content("shared")
    assert not any(row["argv"][0] == "checkpoint" for row in calls(profile))


@pytest.mark.parametrize("directory,advertised,new_name", [
    ("other", "learned", "learned"),
    ("learned", "different", "learned"),
])
def test_selected_name_and_frontmatter_collisions_precede_create(desk, directory, advertised, new_name):
    _, source, shared, _ = desk
    seed(shared, directory, advertised)
    result = invoke(action="create", name="fresh", content=content(new_name))
    assert not result["success"]
    assert "collides" in result["error"]
    assert not (source / "fresh").exists()


def test_symlinked_selection_collision_is_checked(desk):
    _, source, shared, _ = desk
    elsewhere = seed(shared.parent / "catalog", "selected", "learned")
    (shared / "selected").symlink_to(elsewhere, target_is_directory=True)
    result = invoke(action="create", name="fresh", content=content())
    assert not result["success"]
    assert not (source / "fresh").exists()


def test_rename_collision_cannot_write_skill(desk):
    _, source, shared, _ = desk
    seed(source)
    seed(shared, "selected", "taken")
    result = invoke(action="patch", name="learned", old_string="name: learned", new_string="name: taken")
    assert not result["success"]
    assert (source / "learned/SKILL.md").read_text() == content()


@pytest.mark.parametrize("mode", ["checkpoint", "invalid-json"])
def test_bridge_failure_is_visible_and_content_retained(desk, mode):
    profile, source, _, _ = desk
    seed(source)
    # For malformed protocol only fail the checkpoint, not prevalidation.
    if mode == "invalid-json":
        original = personal._bridge
        def bridge(binding, command, name=None):
            if command == "checkpoint":
                (profile / ".bridge-mode").write_text(mode)
            return original(binding, command, name)
        from unittest.mock import patch
        with patch.object(personal, "_bridge", bridge):
            result = invoke(action="edit", name="learned", content=content(body="Revised."))
    else:
        (profile / ".bridge-mode").write_text(mode)
        result = invoke(action="edit", name="learned", content=content(body="Revised."))
    assert not result["success"]
    assert result["content_retained"]
    assert "Durable personal skill checkpoint failed" in result["error"]
    assert (source / "learned/SKILL.md").read_text() == content(body="Revised.")


def test_identity_check_failure_prevents_any_content_write(desk):
    profile, source, _, _ = desk
    (profile / ".bridge-mode").write_text("check")
    result = invoke(action="create", name="learned", content=content())
    assert not result["success"]
    assert "validation failed" in result["error"]
    assert not (source / "learned").exists()


def test_missing_bridge_prevents_bound_write(desk):
    _, source, _, bridge = desk
    bridge.unlink()
    result = invoke(action="create", name="learned", content=content())
    assert not result["success"]
    assert not (source / "learned").exists()


def test_timeout_is_bounded_and_visible(desk, monkeypatch):
    _, source, _, _ = desk
    killed = []
    class TimedOut:
        pid = 99999999
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def communicate(self, timeout=None):
            if timeout is not None:
                assert timeout == 300
                raise subprocess.TimeoutExpired("bridge", timeout)
            return "", ""
        def wait(self, timeout=None):
            assert timeout == 10
        def kill(self):
            killed.append(self.pid)
    monkeypatch.setattr(personal.subprocess, "Popen", lambda *a, **kw: TimedOut())
    monkeypatch.setattr(personal.os, "killpg", lambda pid, sig: killed.append(pid), raising=False)
    result = invoke(action="create", name="learned", content=content())
    assert not result["success"]
    assert killed == [99999999]
    assert not (source / "learned").exists()


def test_invalid_binding_cannot_select_arbitrary_repository(desk):
    profile, source, shared, _ = desk
    marker = profile / ".skillex-personal-source.json"
    binding = json.loads(marker.read_text())
    binding["source"] = str(shared)
    marker.write_text(json.dumps(binding))
    result = invoke(action="create", name="learned", content=content())
    assert not result["success"]
    assert not (source / "learned").exists()
    assert not (shared / "learned").exists()
    assert not calls(profile)


def test_batch_shared_refusal_never_snapshots_or_restores_foreign_tree(desk, monkeypatch):
    _, source, shared, _ = desk
    selected = seed(shared, "selected")
    before = (selected / "SKILL.md").stat().st_ino
    def unexpected_snapshot(*args, **kwargs):
        pytest.fail("shared tree must be refused before snapshot or rollback")
    monkeypatch.setattr("tools.skill_manager_batch._snapshot_skills", unexpected_snapshot)
    result = invoke(operations=[
        {"action": "create", "name": "learned", "content": content()},
        {"action": "patch", "name": "selected", "old_string": "verified", "new_string": "bad"},
    ])
    assert not result["success"]
    assert (selected / "SKILL.md").stat().st_ino == before
    assert not (source / "learned").exists()


def test_no_marker_preserves_ordinary_pm_create_behavior(desk):
    profile, source, shared, bridge = desk
    (profile / ".skillex-personal-source.json").unlink()
    bridge.unlink()
    result = invoke(action="create", name="ordinary", content=content("ordinary"))
    assert result["success"], result
    assert (shared / "ordinary/SKILL.md").exists()
    assert not (source / "ordinary").exists()
    assert "checkpoint" not in result
    assert not calls(profile)


def test_approval_still_stages_without_writing_or_checkpoint(desk, monkeypatch):
    profile, source, _, _ = desk
    monkeypatch.setattr(manager, "_apply_skill_write_gate",
                        lambda *a, **kw: json.dumps({"success": True, "staged": True, "pending_id": "id"}))
    result = invoke(action="create", name="learned", content=content())
    assert result["staged"]
    assert not (source / "learned").exists()
    assert not any(row["argv"][0] == "checkpoint" for row in calls(profile))


def test_security_scan_still_reverts_rejected_content(desk, monkeypatch):
    profile, source, _, _ = desk
    seed(source)
    monkeypatch.setattr(manager, "_security_scan_skill", lambda *a: "security blocked")
    result = invoke(action="edit", name="learned", content=content(body="Rejected."))
    assert not result["success"]
    assert (source / "learned/SKILL.md").read_text() == content()
    assert not any(row["argv"][0] == "checkpoint" for row in calls(profile))
