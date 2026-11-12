"""A dependency generation a live process still runs from is kept, lease or no lease.

Leases cover Hermes's own readers. An MCP server started from a generation's ``venv/bin/python``
or a launchd job whose ``PATH`` names it holds none, and the collectors used to remove that
generation as soon as it was unselected and a day old (``hermes_cli/runtime_state_users.py``).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import runtime_state_users
from hermes_cli.runtime_state import collect_generations
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

    def raises(spellings):
        raise RuntimeError("the probe fell over")

    monkeypatch.setattr(runtime_state_users, "_process_users",
                        raises if failure == "raises" else lambda spellings: None)

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
