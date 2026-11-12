"""A dependency generation a live process still runs from is kept, lease or no lease.

Leases cover Hermes's own readers. An MCP server started from a generation's ``venv/bin/python``
or a launchd job whose ``PATH`` names it holds none, and the collectors used to remove that
generation as soon as it was unselected and a day old (``hermes_cli/runtime_state_users.py``).
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import runtime_state_users
from hermes_cli.runtime_state import collect_generations, lease_directory
from pm.environments import install_state_dir, runtime_facts_path
from pm.install_states import collect_orphan_install_states
from pm.runtime import collect_runtime_generations

LOGGER = "hermes_cli.runtime_state_users"
# Reads the path of a file to hold open from stdin (empty: none), reports ready, then waits.
CHILD = (
    "import sys\n"
    "path = sys.stdin.readline().strip()\n"
    "held = open(path, 'rb') if path else None\n"
    "print('ready', flush=True)\n"
    "sys.stdin.readline()\n"
)


def _collector(kind: str, tmp_path: Path, monkeypatch):
    """``(make, collect)`` for one collector: ``make(name)`` is an old, unselected, unleased
    candidate directory; ``collect()`` runs the collector over every one made."""
    if kind == "dependencies":
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        state = install_state_dir(project)

        def make(name: str) -> Path:
            generation = state / "environments" / name
            generation.mkdir(parents=True)
            (generation / ".lease-managed").touch()
            return generation

        selected = make("selected") / "venv"
        selected.mkdir()
        (selected / "pyvenv.cfg").write_text("version = 3.14\n", encoding="utf-8")
        runtime_facts_path(project).write_text(
            json.dumps({"packages": {"venv": {"environment": str(selected)}}}), encoding="utf-8")
        return make, lambda: collect_generations(project, min_age_seconds=0)
    if kind == "runtime":
        root = tmp_path / "pm-runtime"

        def make(name: str) -> Path:
            generation = root / "generations" / name
            generation.mkdir(parents=True)
            (generation / ".lease-managed").touch()
            (generation / "pm-runtime.json").write_text("{}", encoding="utf-8")
            return generation

        make("selected")
        (root / "selected.json").write_text(json.dumps({"generation": "generations/selected"}), encoding="utf-8")
        return make, lambda: collect_runtime_generations(root)
    installs = tmp_path / "home" / "installs"

    def make(name: str) -> Path:  # an orphan: its checkout sat under the data root and is gone
        state = installs / name
        (state / "inputs").mkdir(parents=True)
        (state / "inputs" / ".project-root").write_text(
            str(tmp_path / "home" / "scratch" / f"gone-{name}"), encoding="utf-8")
        return state

    return make, lambda: collect_orphan_install_states(installs)


@contextmanager
def _running(directory: Path, use: str):
    """A live process that uses *directory* in exactly one way and holds no Hermes lease."""
    interpreter = sys.executable
    env = dict(os.environ)
    cwd = directory.parent
    held = ""
    if use == "argv":  # the kernel resolves the executable past the symlink: only argv names the venv
        (directory / "venv" / "bin").mkdir(parents=True)
        interpreter = str(directory / "venv" / "bin" / "python")
        os.symlink(os.path.realpath(sys.executable), interpreter)
    elif use == "working directory":
        cwd = directory
    elif use == "open file":
        held = str(directory / "held.bin")
        Path(held).write_bytes(b"x")
    else:
        env["PATH"] = f"{directory / 'venv' / 'bin'}{os.pathsep}{env['PATH']}"
    child = subprocess.Popen([interpreter, "-c", CHILD], cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        child.stdin.write(held + "\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "ready"
        yield child
    finally:
        child.communicate("\n", timeout=15)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("use", ["argv", "working directory", "open file", "environment"])
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_generation_a_live_process_runs_from_is_kept(tmp_path, monkeypatch, caplog, kind, use):
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")

    with caplog.at_level(logging.INFO, logger=LOGGER), _running(old, use) as child:
        assert collect() == [idle]
        assert old.is_dir() and not idle.exists()
    kept = [r.getMessage() for r in caplog.records if r.getMessage().startswith("kept")]
    assert len(kept) == 1 and str(old) in kept[0] and f"pid {child.pid}" in kept[0], kept

    assert collect() == [old], "once the process exits the generation is collected as before"


def _debris(old: Path, kind: str) -> list[Path]:
    """What a collector left set aside: beside a generation, inside an install state."""
    where = old if kind == "orphans" else old.parent
    return sorted(p for p in where.iterdir() if runtime_state_users.ASIDE_MARK in p.name)


def _after_the_first_look(monkeypatch, then):
    """Run ``then()`` once, right after the collector's first look at the process table: the gap
    between inspecting a generation and deleting it. Later looks are untouched."""
    real, calls = runtime_state_users.users_of, []

    def users_of(*args, **kwargs):
        users = real(*args, **kwargs)
        if not calls:
            calls.append(then())
        return users

    monkeypatch.setattr(runtime_state_users, "users_of", users_of)
    return calls


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("use", ["argv", "working directory", "open file", "environment"])
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_process_that_starts_between_inspection_and_removal_keeps_it(tmp_path, monkeypatch, caplog, kind, use):
    """The scan said idle; a process starts using the generation before the delete. Deleting on
    the scan's word would pull its files out from under it while it runs."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")

    with caplog.at_level(logging.INFO, logger=LOGGER), ExitStack() as stack:
        started = _after_the_first_look(monkeypatch, lambda: stack.enter_context(_running(old, use)))
        assert collect() == [idle]
        child = started[0]
        assert old.is_dir() and not idle.exists() and child.poll() is None
        assert _debris(old, kind) == [], "what was set aside is back under its own name"
    kept = [r.getMessage() for r in caplog.records if r.getMessage().startswith("kept")]
    assert len(kept) == 1 and str(old) in kept[0] and f"pid {child.pid}" in kept[0], kept
    assert "second look" in kept[0]

    assert collect() == [old], "once the process exits the generation is collected as before"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_lease_taken_between_inspection_and_removal_keeps_it(tmp_path, monkeypatch, kind):
    """A reader outside this PID namespace shows in no process table; its lease is the one signal."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")
    generation = old
    if kind == "orphans":
        generation = old / "environments" / "gen"
        generation.mkdir(parents=True)
        (generation / ".lease-managed").touch()
    releases: list = []

    def take() -> None:
        if not releases:
            releases.append(lease_directory(generation))

    if kind == "orphans":
        import pm.install_states
        real = pm.install_states._leased

        def leased(state):  # the collector checks leases under the install lock; the reader arrives just after
            result = real(state)
            if state == old:
                take()
            return result

        monkeypatch.setattr(pm.install_states, "_leased", leased)
    else:
        _after_the_first_look(monkeypatch, take)

    assert collect() == [idle]
    assert releases and generation.is_dir() and _debris(old, kind) == []
    releases[0]()
    assert collect() == [old]


@pytest.mark.platforms("any")
@pytest.mark.parametrize("failure", ["unreadable", "raises"])
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_second_look_that_fails_puts_everything_back(tmp_path, monkeypatch, caplog, kind, failure):
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")
    real = runtime_state_users.users_of

    def users_of(*args, **kwargs):
        if "own_files" not in kwargs:  # the first look; the second one names the files it may hold
            return real(*args, **kwargs)
        if failure == "raises":
            raise RuntimeError("the probe fell over")
        return None

    monkeypatch.setattr(runtime_state_users, "users_of", users_of)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert collect() == []
    assert old.is_dir() and idle.is_dir()
    assert _debris(old, kind) == [] and _debris(idle, kind) == []
    assert any(r.getMessage().startswith("kept ") for r in caplog.records)

    monkeypatch.setattr(runtime_state_users, "users_of", real)
    assert sorted(collect()) == sorted([old, idle])


@pytest.mark.platforms("any")
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_candidate_is_out_of_reach_while_it_is_looked_at_again(tmp_path, monkeypatch, kind):
    """A process that starts after the rename finds nothing at the old path (ENOENT) instead of
    files that are deleted under it."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")
    for generation in (old, idle):
        (generation / "payload").write_text("x", encoding="utf-8")
    real, reachable, looked_at = runtime_state_users.users_of, [], []

    def users_of(candidates, **kwargs):
        if "own_files" in kwargs:  # the second look: by now the old names must lead nowhere
            for path in candidates:  # the name a process could open it by: the old one
                original = path.with_name(path.name.rpartition(runtime_state_users.ASIDE_MARK)[0] or path.name)
                reachable.append((original / "payload").exists())
                looked_at.append(path)
        return real(candidates, **kwargs)

    monkeypatch.setattr(runtime_state_users, "users_of", users_of)
    assert sorted(collect()) == sorted([old, idle])
    assert reachable == [False, False] and len(looked_at) == 2
    if kind != "orphans":  # what is looked at is the renamed directory, which holds the files
        assert all(runtime_state_users.ASIDE_MARK in path.name for path in looked_at)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_candidate_that_cannot_be_set_aside_is_kept_whole(tmp_path, monkeypatch, caplog, kind):
    """Part-way through setting an install state's contents aside, what moved goes back."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")
    (old / "a").write_text("x", encoding="utf-8")
    (old / "payload").mkdir()
    real = os.rename

    def rename(src, dst, *args, **kwargs):
        if Path(src) in (old, old / "payload"):
            raise PermissionError("cannot move it")
        return real(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch, caplog.at_level(logging.INFO, logger=LOGGER):
        patch.setattr(os, "rename", rename)
        assert collect() == [idle]
    assert (old / "a").is_file() and (old / "payload").is_dir() and _debris(old, kind) == []
    assert any("cannot set" in r.getMessage() for r in caplog.records)
    assert collect() == [old]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_what_cannot_be_put_back_is_never_removed(tmp_path, monkeypatch, caplog, kind):
    """A process turned up and the rename back fails: it stays set aside under a new name, loudly,
    with the process still running from it; the next pass collects it once the process is gone."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")
    real = os.rename

    def rename(src, dst, *args, **kwargs):
        if runtime_state_users.ASIDE_MARK in Path(src).as_posix():
            raise PermissionError("cannot move it back")
        return real(src, dst, *args, **kwargs)

    running = ExitStack()
    try:
        _after_the_first_look(monkeypatch, lambda: running.enter_context(_running(old, "open file")))
        with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger=LOGGER):
            patch.setattr(os, "rename", rename)
            assert collect() == [idle]
        kept = _debris(old, kind)
        assert kept and all(path.exists() for path in kept)
        assert any("could not put it back" in r.getMessage() for r in caplog.records)
    finally:
        running.close()
    assert collect() and not any(path.exists() for path in kept)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_use_by_this_very_process_between_inspection_and_removal_keeps_it(tmp_path, monkeypatch, caplog, kind):
    """The collector's own process is not exempt: a thread of it can start using a candidate (a
    working directory, a file, with no lease) after the first look. Only the install lock it holds
    on purpose is not a use."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")
    opened = []

    def use():
        (old / "held.bin").write_bytes(b"x")
        opened.append(open(old / "held.bin", "rb"))

    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _after_the_first_look(monkeypatch, use)
            assert collect() == [idle]
        assert (old / "held.bin").is_file() and _debris(old, kind) == []
        kept = [r.getMessage() for r in caplog.records if r.getMessage().startswith("kept")]
        assert len(kept) == 1 and f"pid {os.getpid()}" in kept[0] and "second look" in kept[0], kept
    finally:
        for handle in opened:
            handle.close()
    assert collect() == [old]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["dependencies", "runtime"])
def test_a_generation_left_set_aside_is_still_probed_under_its_old_name(tmp_path, monkeypatch, kind):
    """A collector killed between the rename and the delete leaves ``<name>.collecting-<hex>``. A
    process that started from the old path names it in its argv, which the new name does not contain."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old = make("old")
    with _running(old, "argv"):
        left = old.with_name(f"old{runtime_state_users.ASIDE_MARK}deadbeef")
        os.rename(old, left)
        assert collect() == []
        assert left.is_dir()
    assert collect() == [left]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["dependencies", "runtime"])
def test_every_leftover_of_one_generation_answers_to_its_old_name(tmp_path, monkeypatch, kind):
    """Two interrupted collections leave ``old.collecting-a`` and ``old.collecting-b``. A process
    that started from ``old`` names it in argv; neither leftover is idle while it runs, whichever
    of them it actually uses."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old = make("old")
    with _running(old, "argv"):
        first = old.with_name(f"old{runtime_state_users.ASIDE_MARK}aaaaaaaa")
        os.rename(old, first)
        second = make(f"old{runtime_state_users.ASIDE_MARK}bbbbbbbb")
        assert collect() == []
        assert first.is_dir() and second.is_dir()
    assert sorted(collect()) == sorted([first, second])


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["dependencies", "runtime"])
def test_a_leftover_set_aside_again_still_answers_to_its_first_name(tmp_path, monkeypatch, kind):
    """The second look on a leftover renames it once more; a process that names the very first
    path and starts in between is still found, and the leftover goes back where it was."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    left = make(f"old{runtime_state_users.ASIDE_MARK}aaaaaaaa")
    old = left.with_name("old")
    with ExitStack() as stack:
        _after_the_first_look(monkeypatch, lambda: stack.enter_context(_running(old, "argv")))
        assert collect() == []
    assert left.is_dir() and not any(p.name.count(runtime_state_users.ASIDE_MARK) > 1 for p in left.parent.iterdir())
    assert sorted(p.name for p in left.parent.iterdir() if runtime_state_users.ASIDE_MARK in p.name) == [left.name]


@pytest.mark.platforms("posix")
def test_an_interrupt_during_the_second_look_puts_every_candidate_back(tmp_path):
    """The second look hands candidates over only once it has finished with all of them: an
    interrupt in the middle leaves none half-way, set aside and forgotten."""
    first, second = tmp_path / "first", tmp_path / "second"
    for generation in (first, second):
        generation.mkdir()
        (generation / "payload").write_text("x", encoding="utf-8")
    looked_at = []

    def held(aside: Path) -> bool:
        looked_at.append(aside)
        if len(looked_at) == 2:
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt), runtime_state_users.set_aside([first, second], kind="test", held=held):
        pytest.fail("the caller must not be handed anything when the look was interrupted")
    assert (first / "payload").is_file() and (second / "payload").is_file()
    assert [p.name for p in tmp_path.iterdir() if runtime_state_users.ASIDE_MARK in p.name] == []


@pytest.mark.platforms("posix")
def test_what_appeared_in_an_install_state_after_the_second_look_is_not_removed(tmp_path, monkeypatch):
    """Only what was set aside and looked at is deleted. A directory that shows up in the state in
    the meantime (a launch from the recreated checkout) was never looked at: it stays, with the
    record, and the state is retried on the next pass."""
    from pm import install_states

    make, collect = _collector("orphans", tmp_path, monkeypatch)
    state = make("old")
    (state / "environments" / "gen").mkdir(parents=True)
    (state / "environments" / "gen" / "payload").write_text("x", encoding="utf-8")
    real_remove = install_states._remove_contents

    def remove_after_a_launch(path):
        (path / "environments" / "fresh").mkdir(parents=True)
        (path / "environments" / "fresh" / "payload").write_text("new", encoding="utf-8")
        return real_remove(path)

    monkeypatch.setattr(install_states, "_remove_contents", remove_after_a_launch)
    assert collect() == []
    assert (state / "environments" / "fresh" / "payload").read_text(encoding="utf-8") == "new"
    assert (state / "inputs" / ".project-root").is_file()
    assert _debris(state, "orphans") == [] and not (state / "environments" / "gen").exists()

    monkeypatch.setattr(install_states, "_remove_contents", real_remove)
    assert collect() == [state]


@pytest.mark.platforms("any")
@pytest.mark.parametrize("kind", ["dependencies", "runtime"])
def test_a_generation_left_set_aside_is_collected_without_its_marker(tmp_path, monkeypatch, kind):
    """A removal that failed part-way may already have deleted ``.lease-managed``; what a collector
    set aside is collected anyway, where a pre-lease generation without one is kept."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    legacy, left = make("legacy"), make(f"half{runtime_state_users.ASIDE_MARK}cafef00d")
    for generation in (legacy, left):
        (generation / ".lease-managed").unlink()
    assert collect() == [left]
    assert legacy.is_dir()


@pytest.mark.platforms("posix")
def test_a_lease_inside_what_an_install_state_left_set_aside_keeps_it(tmp_path, monkeypatch):
    """A reader in another PID namespace shows in no process table, only by its lease; the contents a
    killed collector moved aside still hold it."""
    make, collect = _collector("orphans", tmp_path, monkeypatch)
    state = make("old")
    generation = state / f"{runtime_state_users.ASIDE_MARK}deadbeef" / "environments" / "gen"
    generation.mkdir(parents=True)
    (generation / ".lease-managed").touch()
    release = lease_directory(generation)

    assert collect() == []
    assert generation.is_dir()
    release()
    assert collect() == [state]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("unit", ["launchd", "systemd", "systemd default dir"])
def test_a_service_definition_naming_a_generation_keeps_it(tmp_path, monkeypatch, unit):
    """Loaded or not, a launchd agent or systemd unit can start a process from it at any time."""
    make, collect = _collector("dependencies", tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")
    account = tmp_path / "account"
    monkeypatch.setenv("HERMES_REAL_HOME", str(account))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    if unit == "launchd":
        path = account / "Library" / "LaunchAgents" / "io.example.job.plist"
        text = f"<dict><key>PATH</key><string>{old}/venv/bin:/usr/bin</string></dict>"
    else:
        if unit == "systemd":
            monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config = Path(os.environ.get("XDG_CONFIG_HOME") or account / ".config")
        path = config / "systemd" / "user" / "job.service.d" / "override.conf"
        text = f"[Service]\nEnvironment=PATH={old}/venv/bin:/usr/bin\n"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")

    assert collect() == [idle]
    assert old.is_dir()
    path.unlink()
    assert collect() == [old]


@pytest.mark.platforms("any")
@pytest.mark.parametrize("failure", ["unreadable", "raises"])
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_probe_that_fails_keeps_everything_and_never_raises(tmp_path, monkeypatch, caplog, kind, failure):
    """An update that has already committed must not fail because the process table was unreadable."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("old"), make("idle")

    def raises(spellings, **kwargs):
        raise RuntimeError("the probe fell over")

    monkeypatch.setattr(runtime_state_users, "_process_users",
                        raises if failure == "raises" else lambda spellings, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert collect() == []
    assert old.is_dir() and idle.is_dir()
    assert any(r.getMessage().startswith("kept 2 ") for r in caplog.records)


@pytest.mark.platforms("linux")
def test_a_proc_that_hides_this_process_is_no_listing(tmp_path, monkeypatch):
    """An empty ``/proc`` (hidepid, a sandbox) reads as 'nothing runs': it must not mean 'idle'."""
    make, collect = _collector("runtime", tmp_path, monkeypatch)
    idle = make("idle")
    monkeypatch.setattr(runtime_state_users, "_proc_views", lambda pid: [])

    assert collect() == []
    assert idle.is_dir()


def _fake(directory: Path, name: str, output: str, status: int) -> None:
    """A stand-in tool printing *output*; ``{me}`` is the pid of whoever ran it (the test process)."""
    script = directory / name
    script.write_text(f"#!/bin/sh\ncat <<EOF\n{output.format(me='$PPID')}\nEOF\nexit {status}\n", encoding="utf-8")
    script.chmod(0o755)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    ("lsof", "ps", "statuses", "expected"),
    [
        pytest.param("p{me}\ncpytest\nfcwd\nn/elsewhere", "{me} python x\n77 srv {old}/venv/bin/python", (0, 0),
                     "pid 77", id="ps-argv"),
        pytest.param("p{me}\ncpytest\nf4\nn/elsewhere\np88\ncmcp\nftxt\nn{old}/venv/lib/x.so", "{me} python x",
                     (0, 0), "pid 88 (mcp)", id="lsof-mapped-file"),
        pytest.param("p{me}\ncpytest\nf4\nn/elsewhere", "{me} python x", (0, 0), {}, id="nothing-runs-from-it"),
        pytest.param("p{me}\ncpytest\nfcwd\nn{old}", "{me} python x", (1, 0), None, id="lsof-exits-non-zero"),
        pytest.param("p{me}\ncpytest\nfcwd\nn/elsewhere", "{me} python x", (0, 3), None, id="ps-exits-non-zero"),
        pytest.param("p1\ncinit\nfcwd\nn/", "{me} python x", (0, 0), None, id="lsof-does-not-list-us"),
        pytest.param("p{me}\ncpytest\nfcwd\nn/", "1 init", (0, 0), None, id="ps-does-not-list-us"),
    ],
)
def test_the_macos_listing_names_users_and_fails_safe(tmp_path, monkeypatch, lsof, ps, statuses, expected):
    """The macOS probe, driven through fake ``lsof``/``ps`` so every host exercises its parsing."""
    old = tmp_path / "old"
    old.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, output, status in (("lsof", lsof, statuses[0]), ("ps", ps, statuses[1])):
        _fake(bin_dir, name, output.replace("{old}", str(old)), status)
    monkeypatch.setattr(runtime_state_users, "_LSOF", (str(bin_dir / "lsof"),))
    monkeypatch.setattr(runtime_state_users, "_PS", (str(bin_dir / "ps"),))

    users = runtime_state_users._listing_users(runtime_state_users._spellings([old]))

    if isinstance(expected, str):
        assert users is not None and expected in users[old]
    else:
        assert users == expected


@pytest.mark.platforms("any")
def test_the_psutil_listing_names_a_working_directory(tmp_path, monkeypatch):
    """Windows takes this path (psutil is a core dependency); every host can exercise it."""
    psutil = pytest.importorskip("psutil")
    old = tmp_path / "old"
    old.mkdir()
    with _running(old, "working directory") as child:
        # Only the two processes the test owns: walking the host's would stat paths under the real home.
        monkeypatch.setattr(psutil, "process_iter", lambda: [psutil.Process(os.getpid()), psutil.Process(child.pid)])
        users = runtime_state_users._psutil_users(runtime_state_users._spellings([old]))
    assert users is not None and f"pid {child.pid}" in users[old]


@pytest.mark.platforms("posix")
def test_this_process_may_hold_only_the_files_it_names_open(tmp_path, monkeypatch):
    """The collector holds the install lock of what it set aside: that file is not a use. Anything
    else this process does inside a candidate still is, a thread of it that began using one included."""
    old = tmp_path / "old"
    old.mkdir()
    lock, other = old / ".install.lock", old / "other"
    lock.touch()
    other.touch()
    me = f"pid {os.getpid()}"
    with open(lock):
        assert me in runtime_state_users.users_of([old])[old]
        assert runtime_state_users.users_of([old], own_files=[lock]) == {}
        with open(other):
            assert me in runtime_state_users.users_of([old], own_files=[lock])[old]
    monkeypatch.chdir(old)
    assert me in runtime_state_users.users_of([old], own_files=[lock])[old]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("fd", "name", "ignored"), [("f9", ".install.lock", True), ("f9", "other", False),
                                                    ("fcwd", "", False), ("fmem", ".install.lock", False),
                                                    ("ftxt", ".install.lock", False)])
def test_the_macos_listing_ignores_only_the_named_open_files(tmp_path, monkeypatch, fd, name, ignored):
    old = tmp_path / "old"
    old.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    named = f"{old}/{name}" if name else str(old)
    _fake(bin_dir, "lsof", f"p{{me}}\ncpytest\n{fd}\nn{named}", 0)
    _fake(bin_dir, "ps", "{me} python x", 0)
    monkeypatch.setattr(runtime_state_users, "_LSOF", (str(bin_dir / "lsof"),))
    monkeypatch.setattr(runtime_state_users, "_PS", (str(bin_dir / "ps"),))
    spellings = runtime_state_users._spellings([old])
    own = frozenset(runtime_state_users._spellings([old / ".install.lock"]))

    assert runtime_state_users._listing_users(spellings)[old].startswith(f"pid {os.getpid()}")
    users = runtime_state_users._listing_users(spellings, ignore=own)
    assert (users == {}) if ignored else (users[old].startswith(f"pid {os.getpid()}"))
