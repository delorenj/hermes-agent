"""PJAN-74: real skill index/prompt walks terminate with finite retained state."""

from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent import skill_walk
from agent.skill_utils import iter_skill_index_files


def _skill(path, name="demo"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Fixture skill.\n---\nInstructions.\n")
    return path


_CHILD = """
import json, os, resource, sys, time
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS, (320 * 1024**2, 320 * 1024**2))
from agent.skill_utils import iter_skill_index_files
from agent.prompt_builder import (build_skills_system_prompt, clear_skills_system_prompt_cache,
                                  _build_skills_manifest, _skills_prompt_snapshot_path)
root = Path(sys.argv[1])
started = time.monotonic()
original = os.scandir
opened = set()
def unique(path):
    st = os.stat(path)
    key = (st.st_dev, st.st_ino)
    assert key not in opened, path
    opened.add(key)
    return original(path)
os.scandir = unique
files = list(iter_skill_index_files(root, 'SKILL.md'))
assert files == sorted(files)
assert len(files) == int(sys.argv[2]), files
opened.clear()
manifest = _build_skills_manifest(root)
assert set(manifest) == {str(p.relative_to(root)) for p in files}, manifest
assert all(len(v) >= 2 for v in manifest.values())
os.scandir = original
clear_skills_system_prompt_cache(clear_snapshot=True)
a = build_skills_system_prompt(skills_dir_override=root)
from agent import prompt_builder as pb
assert json.loads(_skills_prompt_snapshot_path().read_text())['manifest'] == manifest
b = build_skills_system_prompt(skills_dir_override=root)
clear_skills_system_prompt_cache()
original_parse = pb._parse_skill_file
pb._parse_skill_file = lambda p: (_ for _ in ()).throw(AssertionError('disk snapshot not reused'))
try:
    c = build_skills_system_prompt(skills_dir_override=root)
finally:
    pb._parse_skill_file = original_parse
assert a == b == c and 'demo' in a
print(json.dumps({'files': [str(p.relative_to(root)) for p in files],
                  'elapsed_s': time.monotonic()-started,
                  'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  'memory_limit_mib': 320, 'prompt_bytes': len(a)}))
"""


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("topology", ["self", "ancestor", "two-back-links"])
def test_real_index_manifest_cold_and_cached_prompt_are_cycle_safe(tmp_path, topology):
    home = tmp_path / "home"
    root = home / "skills"
    home.mkdir()
    (home / "config.yaml").write_text("skills:\n  project_discovery: false\n")
    one = _skill(root / "one")
    if topology == "self":
        (one / "loop").symlink_to(one, target_is_directory=True)
        expected = 1
    elif topology == "ancestor":
        (one / "back").symlink_to(root, target_is_directory=True)
        expected = 1
    else:
        two = _skill(root / "two", "second")
        (one / "back").symlink_to(two, target_is_directory=True)
        (two / "back").symlink_to(one, target_is_directory=True)
        expected = 2
    result = subprocess.run([sys.executable, "-c", _CHILD, str(root), str(expected)],
                            env={**os.environ, "HERMES_HOME": str(home)},
                            capture_output=True, text=True, timeout=8, check=False)
    assert result.returncode == 0, result.stderr
    assert "cycle or alias" in result.stderr and str(root) in result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["rss_kib"] < 320 * 1024
    print(topology, receipt)


@pytest.mark.platforms("posix")
def test_external_aliases_pruning_and_sorted_paths(tmp_path):
    root = tmp_path / "skills"
    _skill(root / "z-last", "last")
    outside = _skill(tmp_path / "external")
    (root / "a-external").symlink_to(outside, target_is_directory=True)
    (root / "b-alias").symlink_to(outside, target_is_directory=True)
    _skill(root / "node_modules" / "ignored")
    _skill(root / "z-last" / "references" / "ignored")
    _skill(root / "scripts" / "category-skill", "category")
    assert [str(p.relative_to(root)) for p in iter_skill_index_files(root, "SKILL.md")] == [
        "a-external/SKILL.md", "scripts/category-skill/SKILL.md", "z-last/SKILL.md"]


@pytest.mark.parametrize("kind,maximum", [("directories", 4), ("entries", 7), ("results", 3), ("path_bytes", 350), ("depth", 1)])
def test_acyclic_scan_stops_at_budget_during_enumeration(tmp_path, monkeypatch, caplog, kind, maximum):
    root = tmp_path / "skills"
    for n in range(20):
        _skill(root / f"skill-{n:02}" / "nested", f"skill-{n}")
    limits = replace(skill_walk.DEFAULT_LIMITS, **{kind: maximum})
    monkeypatch.setattr(skill_walk, "DEFAULT_LIMITS", limits)
    counts = Counter()
    original = os.scandir

    class Observed:
        def __init__(self, path):
            self.path = path
            self.it = original(path)

        def __enter__(self):
            counts["directories"] += 1
            return self

        def __exit__(self, *args):
            self.it.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.it)
            counts["entries"] += 1
            return entry

    monkeypatch.setattr(os, "scandir", Observed)
    found = list(iter_skill_index_files(root, "SKILL.md"))
    assert found == sorted(found)
    assert kind + " budget" in caplog.text and str(root) in caplog.text
    if kind == "directories":
        assert counts[kind] == maximum
    if kind == "entries":
        assert counts[kind] <= maximum + 1  # one iterator fetch detects overflow; never retained
    if kind == "results":
        assert len(found) == maximum and counts["directories"] < 41
    if kind == "path_bytes":
        assert counts["directories"] < 41 and len(found) < 20
    if kind == "depth":
        assert not found and counts["directories"] == 21


def test_shallow_alias_wins_before_deep_alias(tmp_path, monkeypatch, caplog):
    root = tmp_path / "skills"
    target = _skill(root / "z-target", "shallow")
    deep = root / "a-deep" / "child"
    deep.mkdir(parents=True)
    (deep / "alias").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(skill_walk, "DEFAULT_LIMITS", replace(skill_walk.DEFAULT_LIMITS, depth=2))
    assert [p.relative_to(root).as_posix() for p in iter_skill_index_files(root, "SKILL.md")] == [
        "z-target/SKILL.md"]
    assert "depth budget" in caplog.text


def test_support_dir_does_not_spend_retained_path_budget(tmp_path, monkeypatch, caplog):
    root = tmp_path / "skills"
    _skill(root / "a-first", "first")
    _skill(root / "z-last", "last")
    (root / "a-first" / "references").mkdir()
    limit = sum(len(os.fsencode(p)) for p in [root, root / "a-first", root / "z-last",
                                               root / "a-first" / "SKILL.md", root / "z-last" / "SKILL.md"])
    monkeypatch.setattr(skill_walk, "DEFAULT_LIMITS", replace(skill_walk.DEFAULT_LIMITS, path_bytes=limit))
    assert len(list(iter_skill_index_files(root, "SKILL.md"))) == 2
    assert "path_bytes budget" not in caplog.text


@pytest.mark.platforms("linux")
def test_acyclic_default_directory_budget_in_child_with_memory_limit(tmp_path):
    root = tmp_path / "skills"
    for n in range(skill_walk.DEFAULT_LIMITS.directories):
        _skill(root / f"skill-{n:05}", f"skill-{n}")
    child = """
import resource, sys
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS, (320 * 1024**2, 320 * 1024**2))
from agent.skill_utils import iter_skill_index_files
files = list(iter_skill_index_files(Path(sys.argv[1]), 'SKILL.md'))
assert len(files) == 4095
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
"""
    result = subprocess.run([sys.executable, "-c", child, str(root)], capture_output=True, text=True,
                            timeout=12, check=False)
    assert result.returncode == 0, result.stderr
    assert "directories budget" in result.stderr and str(root) in result.stderr
    assert int(result.stdout.splitlines()[-1]) < 320 * 1024


def test_default_entry_budget_bounds_a_flat_acyclic_directory(tmp_path, caplog):
    for n in range(skill_walk.DEFAULT_LIMITS.entries + 1):
        (tmp_path / f"file-{n}").touch()
    assert list(iter_skill_index_files(tmp_path, "SKILL.md")) == []
    assert "entries budget (65536)" in caplog.text
