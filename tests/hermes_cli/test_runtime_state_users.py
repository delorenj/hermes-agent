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
from types import SimpleNamespace
import time

import pytest

from hermes_cli import runtime_state_users
from hermes_cli.runtime_state import collect_generations, lease_directory
from pm.environments import install_state_dir, runtime_facts_path
from pm.install_states import collect_orphan_install_states
from pm.runtime import collect_runtime_generations


@pytest.fixture(autouse=True)
def _patient_scan(monkeypatch):
    """The CI host is busy; a probe that gave up on it would keep everything and fail tests that
    are not about giving up. The tests that are set their own limit."""
    monkeypatch.setattr(runtime_state_users, "SCAN_ROUNDS", 1000, raising=False)


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


# Holds a file it is told about on stdin; on "fork" it forks a child that keeps the descriptor,
# closes its own, and prints the child's pid; on the next line both exit.
FORKER = (
    "import os, sys\n"
    "fd = os.open(sys.stdin.readline().strip(), os.O_RDONLY)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.readline()\n"
    "r, w = os.pipe()\n"
    "child = os.fork()\n"
    "if child == 0:\n"
    "    os.close(w)\n"
    "    os.read(r, 1)\n"
    "    os._exit(0)\n"
    "os.close(r)\n"
    "os.close(fd)\n"
    "print(child, flush=True)\n"
    "sys.stdin.readline()\n"
    "os.close(w)\n"
    "os.waitpid(child, 0)\n"
)


class _Listed(list):
    """A materialised ``os.scandir`` result that still works as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("kind", ["dependencies", "runtime", "orphans"])
def test_a_child_forked_while_the_second_look_reads_the_table_keeps_it(tmp_path, monkeypatch, caplog, kind):
    """A process began using the generation after the first look. Right after the second look lists
    the process table, it forks and leaves its descriptor to the child, which that listing missed,
    then closes its own before its turn comes: read on that one listing, nobody uses the generation."""
    make, collect = _collector(kind, tmp_path, monkeypatch)
    old, idle = make("a-old"), make("idle")  # install states are handled in name order: old goes first
    held = old / "held.bin"
    held.write_bytes(b"x")
    forker = subprocess.Popen([sys.executable, "-c", FORKER], cwd=tmp_path, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, encoding="utf-8")
    armed: list = []
    child: list[int] = []

    def start() -> None:  # the gap after the first look: the process starts using the generation
        forker.stdin.write(f"{held}\n")
        forker.stdin.flush()
        assert forker.stdout.readline().strip() == "ready"
        armed.append(True)

    def fork() -> None:
        forker.stdin.write("fork\n")
        forker.stdin.flush()
        child.append(int(forker.stdout.readline()))

    real_scandir = os.scandir

    def scandir(path="."):
        if path == "/proc" and armed:  # the second look's listing, taken before the fork
            armed.clear()
            listed = _Listed(real_scandir(path))
            fork()
            return listed
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _after_the_first_look(monkeypatch, start)
            assert collect() == [idle]
        assert child, "the second look never listed the process table"
        assert old.is_dir() and held.is_file() and _debris(old, kind) == []
        kept = [r.getMessage() for r in caplog.records if r.getMessage().startswith("kept")]
        assert len(kept) == 1 and f"pid {child[0]}" in kept[0] and "second look" in kept[0], kept
    finally:
        forker.communicate("\n", timeout=15)

    assert collect() == [old], "once both processes exit the generation is collected as before"


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
@pytest.mark.parametrize("keep", [None, frozenset({"inputs"})], ids=["generation", "state contents"])
def test_an_interrupt_right_after_a_rename_still_puts_it_back(tmp_path, monkeypatch, keep):
    """The target is recorded before the move, so an interrupt that lands after the rename but
    before the collector would have noted it still leaves the candidate where it was."""
    old = tmp_path / "old"
    (old / "inputs").mkdir(parents=True)
    (old / "payload").write_text("x", encoding="utf-8")
    real, calls = os.rename, []

    def rename_then_interrupt(src, dst):
        real(src, dst)
        calls.append(dst)
        if len(calls) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(runtime_state_users.os, "rename", rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt), runtime_state_users.set_aside([old], kind="test", keep=keep):
        pytest.fail("nothing is handed over after an interrupt")
    monkeypatch.undo()
    assert (old / "payload").is_file() and (old / "inputs").is_dir()
    assert [p.name for p in tmp_path.iterdir() if runtime_state_users.ASIDE_MARK in p.name] == []
    assert [p.name for p in old.iterdir() if runtime_state_users.ASIDE_MARK in p.name] == []


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("leftover", ["not empty", "empty"])
@pytest.mark.parametrize("keep", [None, frozenset({"inputs"})], ids=["generation", "state contents"])
def test_a_set_aside_name_that_is_taken_is_never_reused_or_touched(tmp_path, monkeypatch, keep, leftover):
    """The name a candidate is set aside under is one nothing has. A leftover that already carries
    the first name drawn stays exactly as it was, through the move and the put-back (an empty one
    too, which a POSIX ``rename`` of a generation onto it would silently replace)."""
    old = tmp_path / "old"
    (old / "inputs").mkdir(parents=True)
    (old / "payload").write_text("x", encoding="utf-8")
    taken = old / f"{runtime_state_users.ASIDE_MARK}aaaaaaaa" if keep else tmp_path / f"old{runtime_state_users.ASIDE_MARK}aaaaaaaa"
    taken.mkdir()
    if leftover == "not empty":
        (taken / "theirs").write_text("not ours", encoding="utf-8")
    identity = (taken.stat().st_dev, taken.stat().st_ino)
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(runtime_state_users.secrets, "token_hex", lambda _n: next(tokens))

    with runtime_state_users.set_aside([old], kind="test", held=lambda _aside: True, keep=keep) as idle:
        assert idle == {}  # held: put back
    assert (taken.stat().st_dev, taken.stat().st_ino) == identity
    assert sorted(p.name for p in taken.iterdir()) == (["theirs"] if leftover == "not empty" else [])
    assert (old / "payload").is_file() and not (old / "theirs").exists()
    assert sorted(p.name for p in taken.parent.iterdir() if runtime_state_users.ASIDE_MARK in p.name) == [taken.name]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("contents", [False, True], ids=["empty", "not empty"])
def test_a_put_back_never_moves_onto_a_name_that_is_in_use(tmp_path, caplog, contents):
    """If the candidate's own name exists, the move was not made by us (an interrupt before it):
    whatever sits at the set-aside name is left alone rather than moved onto it. An empty directory
    is the case that matters: POSIX ``rename`` would replace it without a word."""
    old, other = tmp_path / "old", tmp_path / f"old{runtime_state_users.ASIDE_MARK}cccccccc"
    old.mkdir()
    if contents:
        (old / "mine").write_text("mine", encoding="utf-8")
    other.mkdir()
    (other / "theirs").write_text("theirs", encoding="utf-8")
    identity = (old.stat().st_dev, old.stat().st_ino)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        runtime_state_users._put_back(old, other, kind="test", keep=None)
    assert (old.stat().st_dev, old.stat().st_ino) == identity
    assert sorted(p.name for p in old.iterdir()) == (["mine"] if contents else [])
    assert sorted(p.name for p in other.iterdir()) == ["theirs"]
    assert any("left alone" in r.getMessage() for r in caplog.records)


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


def _fake_calls(directory: Path, name: str, outputs: list[str], *, when: str = "", otherwise: str = "") -> None:
    """A stand-in tool whose Nth call with *when* in its arguments prints ``outputs[N]`` (the last
    one repeats) and whose other calls print *otherwise*; ``{me}`` is the pid of whoever ran it."""
    for index, output in enumerate([*outputs, outputs[-1]]):
        (directory / f"{name}.{index if index < len(outputs) else 'last'}").write_text(output + "\n", encoding="utf-8")
    (directory / f"{name}.otherwise").write_text(otherwise + "\n", encoding="utf-8")
    script = directory / name
    script.write_text(
        "#!/bin/sh\n"
        f'case "$*" in\n'
        f'  *{when}*) n=$(cat "{directory}/{name}.calls" 2>/dev/null || echo 0)\n'
        f'     echo $((n + 1)) > "{directory}/{name}.calls"\n'
        f'     out="{directory}/{name}.$n"; [ -f "$out" ] || out="{directory}/{name}.last" ;;\n'
        f'  *) out="{directory}/{name}.otherwise" ;;\n'
        "esac\n"
        'sed "s/{me}/$PPID/g" "$out"\n',
        encoding="utf-8")
    script.chmod(0o755)


@pytest.mark.platforms("posix")
def test_the_macos_listing_reads_a_process_born_while_it_read_the_table(tmp_path, monkeypatch):
    """``lsof`` and ``ps`` take their own snapshots. A process born after them (a child that took
    over its parent's descriptor) is named by the next ``ps`` listing, and the probe reads again."""
    old = tmp_path / "old"
    old.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    me = "p{me}\ncpytest\nfcwd\nn/elsewhere"
    _fake_calls(bin_dir, "lsof", [me, f"{me}\np99\ncchild\nf3\nn{old}/held.bin"])
    _fake_calls(bin_dir, "ps", ["{me} Thu Nov 12 10:00:00 2026", "{me} Thu Nov 12 10:00:00 2026\n99 Thu Nov 12 10:00:01 2026"],
                when="lstart", otherwise="{me} python x\n99 child")
    monkeypatch.setattr(runtime_state_users, "_LSOF", (str(bin_dir / "lsof"),))
    monkeypatch.setattr(runtime_state_users, "_PS", (str(bin_dir / "ps"),))

    users = runtime_state_users._listing_users(runtime_state_users._spellings([old]))

    assert users is not None and users[old].startswith("pid 99 (child)")
    assert (bin_dir / "lsof.calls").read_text(encoding="utf-8").strip() == "2"
    assert (bin_dir / "ps.calls").read_text(encoding="utf-8").strip() == "3", "settled on a listing with nobody new"


@pytest.mark.platforms("posix")
def test_a_process_table_that_never_settles_is_no_answer(tmp_path, monkeypatch, caplog):
    """A host that keeps starting processes through every listing gets no verdict: everything is kept."""
    old = tmp_path / "old"
    old.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    listings = ["{me} T0" + "".join(f"\n{100 + n} T{n}" for n in range(rounds)) for rounds in range(6)]
    _fake_calls(bin_dir, "lsof", ["p{me}\ncpytest\nfcwd\nn/elsewhere"])
    _fake_calls(bin_dir, "ps", listings, when="lstart", otherwise="{me} python x")
    monkeypatch.setattr(runtime_state_users, "_LSOF", (str(bin_dir / "lsof"),))
    monkeypatch.setattr(runtime_state_users, "_PS", (str(bin_dir / "ps"),))
    monkeypatch.setattr(runtime_state_users, "SCAN_ROUNDS", 4)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert runtime_state_users._listing_users(runtime_state_users._spellings([old])) is None
    assert any("kept starting" in r.getMessage() for r in caplog.records)


@pytest.mark.platforms("linux")
def test_a_proc_that_never_settles_is_no_answer(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    real, born = runtime_state_users._proc_table, []

    def table(budget):
        born.append(str(4_000_000_000 + len(born)))  # a pid nothing has, new on every listing
        return [*real(budget), *born]

    monkeypatch.setattr(runtime_state_users, "_proc_table", table)
    monkeypatch.setattr(runtime_state_users, "SCAN_ROUNDS", 4)
    assert runtime_state_users._proc_users(runtime_state_users._spellings([old])) is None


@pytest.mark.platforms("any")
def test_the_psutil_listing_names_a_working_directory(tmp_path, monkeypatch):
    """Windows takes this path (psutil is a core dependency); every host can exercise it."""
    psutil = pytest.importorskip("psutil")
    old = tmp_path / "old"
    old.mkdir()
    with _running(old, "working directory") as child:
        # Only the two processes the test owns: walking the host's would stat paths under the real home.
        monkeypatch.setattr(psutil, "pids", lambda: [os.getpid(), child.pid])
        users = runtime_state_users._psutil_users(runtime_state_users._spellings([old]))
    assert users is not None and f"pid {child.pid}" in users[old]


@pytest.mark.platforms("any")
def test_the_psutil_listing_reads_a_pid_that_went_to_a_new_process(tmp_path, monkeypatch):
    """psutil's ``process_iter`` hands back the objects it cached, start time included, so a pid
    reused by a new process looks like the old one. Each listing builds its Process objects anew."""
    old = tmp_path / "old"
    old.mkdir()
    held = str(old / "held.bin")
    listings = []

    class Error(Exception):
        pass

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self.round = len(listings)  # which process holds this pid when the object is made

        def create_time(self):
            return 1.0 if self.round <= 1 else 2.0

        def cmdline(self):
            return ["python"]

        def open_files(self):
            return [SimpleNamespace(path=held)] if self.pid == 7 and self.round >= 2 else []

        def exe(self):
            return "/bin/python"

        def cwd(self):
            return "/"

        def memory_maps(self):
            return []

        def environ(self):
            return {}

    cached = {}

    def pids():
        listings.append(True)
        return [os.getpid(), 7]

    def process_iter():  # what the old listing used: objects kept from the first look
        return [cached.setdefault(pid, Process(pid)) for pid in pids()]

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Error=Error, Process=Process, pids=pids,
                                                               process_iter=process_iter))
    users = runtime_state_users._psutil_users(runtime_state_users._spellings([old]))
    assert users is not None and users[old] == "pid 7: open file"


@pytest.mark.platforms("any")
def test_the_psutil_listing_reads_a_process_that_hides_its_start_time(tmp_path, monkeypatch):
    """Access denied on the start time (a system process on Windows) is not a reason to skip the process."""
    old = tmp_path / "old"
    old.mkdir()

    class Error(Exception):
        pass

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            raise Error("access denied")

        def cmdline(self):
            return ["python"]

        def open_files(self):
            return [SimpleNamespace(path=str(old / "held.bin"))] if self.pid == 7 else []

        def __getattr__(self, name):
            return list

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Error=Error, Process=Process,
                                                               pids=lambda: [os.getpid(), 7]))
    users = runtime_state_users._psutil_users(runtime_state_users._spellings([old]))
    assert users is not None and users[old] == "pid 7: open file"


def test_a_listing_that_comes_back_empty_is_no_answer():
    """The table was there a moment ago: an empty relisting is a view that vanished, not a settled one."""
    lists, reads = [[1], []], []
    assert runtime_state_users._until_settled(lambda _budget: lists.pop(0),
                                              lambda fresh, _budget: reads.append(fresh) or True) is False
    assert reads == [[1]]


def test_a_probe_out_of_time_is_no_answer_even_when_the_table_settled(monkeypatch):
    """A slow read that is followed by a quiet listing still ran past the time the install lock allows."""
    monkeypatch.setattr(runtime_state_users, "PROBE_TIMEOUT_SECONDS", 0.05)
    slow = lambda fresh, _budget: time.sleep(0.2) or True
    assert runtime_state_users._until_settled(lambda _budget: [1], slow) is False
    monkeypatch.setattr(runtime_state_users, "PROBE_TIMEOUT_SECONDS", 30)
    assert runtime_state_users._until_settled(lambda _budget: [1], lambda fresh, _budget: True) is True


@pytest.mark.platforms("posix")
def test_the_tools_a_probe_runs_share_its_time(tmp_path, monkeypatch):
    """One tool that outlasts what is left of the probe's time ends the probe: no answer, not a wait."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "slow").write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    (bin_dir / "slow").chmod(0o755)
    monkeypatch.setattr(runtime_state_users, "PROBE_TIMEOUT_SECONDS", 0.3)
    started = time.monotonic()
    assert runtime_state_users._output([str(bin_dir / "slow")], runtime_state_users._Budget()) is None
    assert time.monotonic() - started < 3


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
