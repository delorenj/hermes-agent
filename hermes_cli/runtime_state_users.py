"""What still uses a dependency generation that holds no Hermes lease.

Leases (:func:`hermes_cli.runtime_state.lease_directory`) cover Hermes's own readers. A process
started some other way holds none: an MCP server configured with a generation's
``venv/bin/python``, a launchd job or systemd unit whose ``PATH`` names its ``venv/bin``. Removing
that generation pulls the interpreter and packages out from under it. On a Mac, an update would
have removed the unselected, weeks-old, unleased generation a team-inbox gateway's MCP server ran
from and its launchd job's ``PATH`` named. So before removing anything, the collectors ask
:func:`unused`, which keeps every candidate when it cannot list what runs from them.

Stdlib only, like :mod:`hermes_cli.runtime_state`: the update tail collects from an ``-I -S``
bootstrap interpreter. One probe per OS family (``/proc`` on Linux, ``lsof`` + ``ps`` on macOS and
the BSDs, psutil on Windows when it imports), so every route that collects on a host answers the
same way whatever interpreter it runs in. Only ``hermes_constants`` (the account home) is imported
besides the standard library: a closure that ships ``runtime_state.py`` ships that too.
"""
from __future__ import annotations

from collections.abc import Collection, Iterator
import logging
import os
from pathlib import Path
import subprocess

LOG = logging.getLogger(__name__)

# The collectors ask while holding the install lock, which readers wait on for at most
# INSTALL_LOCK_TIMEOUT_SECONDS before carrying on without it; a slower listing is no answer.
PROBE_TIMEOUT_SECONDS = 15
# The OS's own tools, by absolute path: a ``PATH`` entry can shadow them, and a launchd job's
# ``PATH`` does not include ``/usr/sbin``. Neither is on Windows, where psutil answers instead.
_LSOF = ("/usr/sbin/lsof", "/usr/bin/lsof")
_PS = ("/bin/ps", "/usr/bin/ps")


def unused(candidates: Collection[Path], *, kind: str) -> list[Path]:
    """The candidates nothing uses, in order; each one kept is logged with the reason.

    Empty when the process table cannot be read, or reading it fails: a removed generation is
    unrecoverable for a process running from it, a kept one only costs disk until the next pass.
    Never raises: reclaiming space must not fail an update that has already committed.
    """
    if not candidates:
        return []
    try:
        users = users_of(candidates)
    except Exception:  # whatever the probe trips on, the candidates are kept
        LOG.warning("kept %d %s(s): probing the processes that may run from them failed",
                    len(candidates), kind, exc_info=True)
        return []
    if users is None:
        LOG.warning("kept %d %s(s): cannot list the processes that may run from them",
                    len(candidates), kind)
        return []
    for path, reason in users.items():
        LOG.info("kept %s %s: %s", kind, path, reason)
    return [path for path in candidates if path not in users]


def users_of(candidates: Collection[Path]) -> dict[Path, str] | None:
    """Why each candidate directory is in use; a candidate that is not is absent.

    In use: a live process's executable, working directory, open or mapped files, argv or
    environment name a path inside it, or one of this account's launchd agents or systemd user
    units names it (program, arguments, ``PATH``; loaded or not, since it can start at any time).
    None when the processes cannot be listed.
    """
    spellings = _spellings(candidates)
    users = _process_users(spellings)
    if users is not None:
        for path, reason in _unit_users(spellings).items():
            users.setdefault(path, reason)
    return users


def _spellings(candidates: Collection[Path]) -> dict[bytes, Path]:
    """Each candidate as given and as the kernel reports it (``/tmp`` is ``/private/tmp`` on macOS)."""
    return {os.fsencode(os.path.normcase(spelling)): path for path in candidates
            for spelling in (os.path.abspath(path), os.path.realpath(path))}


def _matches(spellings: dict[bytes, Path], users: dict[Path, str], blob: bytes) -> list[Path]:
    """Candidates not yet in use that ``blob`` names. Plain containment is exact here: generations,
    PM runtime generations and install states are all fixed-width ids, none a prefix of another."""
    return [path for spelling, path in spellings.items() if path not in users and spelling in blob]


def _process_users(spellings: dict[bytes, Path]) -> dict[Path, str] | None:
    if os.path.isdir("/proc/self/fd"):
        return _proc_users(spellings)
    if os.name == "posix":
        return _listing_users(spellings)
    return _psutil_users(spellings)


def _proc_users(spellings: dict[bytes, Path]) -> dict[Path, str] | None:
    """Linux: what ``/proc`` shows of every process.

    Another user's process shows only its argv; one that exits mid-scan shows nothing more. The
    scan counts only if it saw this process's own argv: a ``/proc`` that hides even that is no
    listing at all.
    """
    try:
        pids = [entry.name for entry in os.scandir("/proc") if entry.name.isdigit()]
    except OSError:
        return None
    own = str(os.getpid())
    users: dict[Path, str] = {}
    witnessed = False
    for pid in pids:
        for what, blob in _proc_views(pid):
            witnessed = witnessed or (pid == own and what == "argv")
            for path in _matches(spellings, users, blob):
                users[path] = f"pid {pid} ({_comm(pid)}): {what}"
    return users if witnessed else None


def _proc_views(pid: str) -> list[tuple[str, bytes]]:
    base = f"/proc/{pid}"
    views = (
        ("executable", _link(f"{base}/exe")),
        ("working directory", _link(f"{base}/cwd")),
        ("open file", _fd_targets(f"{base}/fd")),
        ("mapped file", _read(f"{base}/maps")),
        ("argv", _read(f"{base}/cmdline")),
        ("environment", _read(f"{base}/environ")),
    )
    return [(what, blob) for what, blob in views if blob]


def _link(path: str) -> bytes | None:
    try:
        return os.fsencode(os.readlink(path))
    except OSError:
        return None


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _fd_targets(path: str) -> bytes | None:
    try:
        with os.scandir(path) as fds:
            return b"\0".join(target for fd in fds if (target := _link(fd.path)))
    except OSError:
        return None


def _comm(pid: str) -> str:
    return (_read(f"/proc/{pid}/comm") or b"?").decode("utf-8", "replace").strip()


def _listing_users(spellings: dict[bytes, Path]) -> dict[Path, str] | None:
    """macOS and the BSDs: ``lsof`` for executables, cwds, open and mapped files, ``ps`` for argv
    and environment (a venv's ``bin/python`` is a symlink: ``lsof`` reports the interpreter it
    resolves to, only argv still names the venv).

    Each listing counts only if it exits 0 and includes this process.
    """
    lsof, ps = (next((path for path in paths if os.access(path, os.X_OK)), None) for paths in (_LSOF, _PS))
    if lsof is None or ps is None:
        return None
    files = _output([lsof, "-n", "-P", "-w", "-F", "pcfn"])
    table = _output([ps, "-A", "-ww", "-E", "-o", "pid=", "-o", "command="])
    if files is None or table is None:
        return None
    users: dict[Path, str] = {}
    own = str(os.getpid())
    if _scan_lsof(files, spellings, users, own) and _scan_ps(table, spellings, users, own):
        return users
    return None


def _output(argv: list[str]) -> bytes | None:
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


_LSOF_STATE = frozenset((b"p", b"c", b"f"))


def _lsof_files(listing: bytes) -> Iterator[tuple[str, str, str, bytes]]:
    """``(pid, command, fd, name)`` for each file ``lsof -F pcfn`` lists."""
    state: dict[bytes, bytes] = {}
    for line in listing.split(b"\n"):
        tag, value = line[:1], line[1:]
        if tag == b"p":
            state = {}  # a new process starts with no command or descriptor
        if tag in _LSOF_STATE:
            state[tag] = value
        elif tag == b"n":
            pid, command, fd = (state.get(key, b"").decode("utf-8", "replace") for key in (b"p", b"c", b"f"))
            yield pid, command, fd, value


def _scan_lsof(listing: bytes, spellings: dict[bytes, Path], users: dict[Path, str], own: str) -> bool:
    """Record what ``lsof`` names; True when the listing includes ``own`` (it always lists a cwd)."""
    witnessed = False
    for pid, command, fd, name in _lsof_files(listing):
        witnessed = witnessed or pid == own
        for path in _matches(spellings, users, name):
            users[path] = f"pid {pid} ({command}): file, fd {fd}"
    return witnessed


def _scan_ps(table: bytes, spellings: dict[bytes, Path], users: dict[Path, str], own: str) -> bool:
    """Record what ``ps -E -o pid= -o command=`` names; True when the table includes ``own``."""
    witnessed = False
    for line in table.split(b"\n"):
        pid, _, rest = line.strip().partition(b" ")
        witnessed = witnessed or pid.decode("ascii", "replace") == own
        for path in _matches(spellings, users, rest):
            users[path] = f"pid {pid.decode('ascii', 'replace')}: argv or environment"
    return witnessed


_PSUTIL_VIEWS = (("exe", "executable"), ("cwd", "working directory"), ("open_files", "open file"),
                 ("memory_maps", "mapped file"), ("cmdline", "argv"), ("environ", "environment"))


def _psutil_users(spellings: dict[bytes, Path]) -> dict[Path, str] | None:
    """Windows: psutil, when this interpreter has it (the ``-I -S`` bootstrap does not)."""
    try:
        import psutil
    except ImportError:
        return None
    users: dict[Path, str] = {}
    witnessed = False
    for process in psutil.process_iter():
        for what, blob in _psutil_views(process, psutil):
            witnessed = witnessed or (process.pid == os.getpid() and what == "argv")
            for path in _matches(spellings, users, blob):
                users[path] = f"pid {process.pid}: {what}"
    return users if witnessed else None


def _psutil_views(process, psutil) -> Iterator[tuple[str, bytes]]:
    for method, what in _PSUTIL_VIEWS:
        try:
            value = getattr(process, method)()
        except (psutil.Error, OSError, NotImplementedError, AttributeError):
            continue  # gone, another user's, or (memory_maps on macOS) not offered by this OS
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, dict):
            items = [f"{key}={item}" for key, item in value.items()]
        else:
            items = [getattr(item, "path", item) for item in value]
        yield what, b"\0".join(os.fsencode(os.path.normcase(str(item))) for item in items)


def _unit_users(spellings: dict[bytes, Path]) -> dict[Path, str]:
    users: dict[Path, str] = {}
    for unit, text in _service_definitions():
        for path in _matches(spellings, users, text):
            users[path] = f"{unit} names it"
    return users


def _service_definitions() -> Iterator[tuple[str, bytes]]:
    """This account's launchd agents and systemd user units with their drop-ins, as raw bytes.

    Raw, not parsed: a path is ASCII in an XML or a binary plist alike, and a file that does not
    parse is still the one launchd or systemd would be told to load. The account home, not the
    process HOME a profile may isolate (``hermes_cli.gateway.user_systemd_unit_dir``).
    """
    from hermes_constants import get_real_home

    home = Path(get_real_home())
    agents = home / "Library" / "LaunchAgents"
    config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    units = (Path(config) if config else home / ".config") / "systemd" / "user"
    found = [("launchd agent", path) for path in sorted(agents.glob("*.plist"))]
    found += [("systemd user unit", Path(where, name))
              for where, _dirs, names in os.walk(units) for name in sorted(names)]
    for label, path in found:
        # Regular files only: reading a FIFO would block the collector under the install lock.
        if os.path.isfile(path) and (text := _read(str(path))) is not None:
            yield f"{label} {path.name}", text
