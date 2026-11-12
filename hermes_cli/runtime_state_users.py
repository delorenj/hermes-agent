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

:func:`unused` answers for the moment it looked, and removing a large generation takes long
enough for a process to start using an idle one in between. So a collector removes in two phases:
:func:`set_aside` renames what :func:`unused` cleared away from the path a process would open
it by, looks again, and only then yields what is still idle for deletion; anything that turned
out to be in use is renamed back. The new name is random and nothing configured names it, but it
is not hidden: a process that goes looking in the directory by hand can still reach it. What a
probe cannot see it cannot protect: another user's process shows only its argv, and a process
whose only link to a generation is code already imported into memory shows nothing at all.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterator
import contextlib
import logging
import os
from pathlib import Path
import secrets
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


# Where a collector sets a candidate aside: ``<generation>.collecting-<hex>`` beside it, or
# ``<state>/.collecting-<hex>`` holding a state's contents.
ASIDE_MARK = ".collecting-"


@contextlib.contextmanager
def set_aside(candidates: Collection[Path], *, kind: str, held: Callable[[Path], bool] | None = None,
              keep: Collection[str] | None = None) -> Iterator[dict[Path, Path]]:
    """Second phase of a collect: set what :func:`unused` cleared aside, look again, and yield
    ``{candidate: where it is now}`` for what is still idle, for the caller to delete.

    :func:`unused` answers for the instant it looked; deleting a large generation comes seconds
    later, and a process may have started using it by then (an MCP server launched from its
    ``venv/bin/python``) with nothing the collector could see. Renaming first closes that: a
    process that starts afterwards finds nothing at the old path, and one that started in between
    shows up in a second look (its files and working directory under the new name, its argv and
    environment under the old one, which the new name answers to), or through ``held`` (a lease
    taken in the gap; the one signal that crosses a PID namespace). What is in use is renamed back
    and never yielded. The new name is random and named by no configuration, and Hermes picks a
    generation by its recorded path, never by listing the directory, so nothing reaches it after the
    second look but a process that goes looking for it by hand.

    Never raises for the checks: a candidate that cannot be renamed, a listing that fails, or a
    ``held`` that raises keeps the candidate. Whatever is not yielded is renamed back on the way out,
    also when the caller raises or is interrupted (one exception: an interrupt that lands in the
    few instructions between the second look's verdict and the hand-over leaves that candidate set
    aside; it was judged idle, and a later pass collects it). What is yielded is the caller's to
    delete, and what it leaves behind keeps its new name, collected by a later pass (which still
    probes the name it had).

    ``keep=None`` renames each candidate beside itself. With ``keep`` (names that stay put, which
    this process may hold open: an install lock whose path a launcher may recreate) the
    candidate's own directory stays where it is and its other contents move into a
    ``.collecting-*`` directory inside it; leftovers of an earlier pass stay where they are.
    """
    pending: dict[Path, Path] = {}
    try:
        for path in candidates:
            pending[path] = target = _aside_name(path, keep)  # known before the move, so an interrupt in it is undone
            if not _move_aside(path, target, keep, kind=kind):
                del pending[path]
        idle = _second_look(pending, kind=kind, held=held, keep=keep)
        for path in idle:  # only now are they the caller's: until here an interrupt puts them back
            del pending[path]
        yield idle
    finally:
        for path in list(pending):
            _put_back(path, pending[path], kind=kind, keep=keep)
            del pending[path]


def _aside_name(path: Path, keep: Collection[str] | None) -> Path:
    """A name nothing has: what is found there afterwards is ours, or the move never happened."""
    while True:
        token = secrets.token_hex(4)
        target = (path.with_name(f"{_original(path.name)}{ASIDE_MARK}{token}") if keep is None
                  else path / f"{ASIDE_MARK}{token}")
        if not os.path.lexists(target):
            return target


def _move_aside(path: Path, target: Path, keep: Collection[str] | None, *, kind: str) -> bool:
    """Move ``path`` (or its contents, with ``keep``) to ``target``; False, with nothing moved, when it cannot."""
    if keep is None:
        try:
            os.rename(path, target)
        except OSError as exc:
            LOG.info("kept %s %s: cannot set it aside (%s)", kind, path, exc)
            return False
        return True
    try:
        target.mkdir()
        for child in sorted(path.iterdir()):
            if child.name not in keep and not child.name.startswith(ASIDE_MARK):
                os.rename(child, target / child.name)
    except BaseException as exc:  # part-way: what moved goes back, an interrupt still propagates
        _put_back(path, target, kind=kind, keep=keep)
        if not isinstance(exc, OSError):
            raise
        LOG.info("kept %s %s: cannot set its contents aside (%s)", kind, path, exc)
        return False
    return True


def _second_look(pending: dict[Path, Path], *, kind: str, held: Callable[[Path], bool] | None,
                 keep: Collection[str] | None) -> dict[Path, Path]:
    """Of the candidates in ``pending`` (moved by now), return those still idle; the rest are put
    back and dropped from ``pending``. Whatever is still in ``pending`` when this raises, or when
    it returns, is the caller's to put back or hand on."""
    if not pending:
        return {}
    # A generation is probed under its new name (which answers to the old one too); a state never
    # left its place, and this process may hold the files in ``keep`` open.
    probes = {(path if keep is not None else target): path for path, target in pending.items()}
    own = [path / name for path in pending for name in keep or ()]
    try:
        users = users_of(list(probes), own_files=own)
    except Exception:
        LOG.warning("kept %d %s(s): probing the processes that may run from them failed",
                    len(pending), kind, exc_info=True)
        users = None
    if users is None:
        LOG.warning("kept %d %s(s): cannot list the processes that may run from them", len(pending), kind)
    idle: dict[Path, Path] = {}
    for probe, path in probes.items():
        target = pending[path]
        reason = None if users is None else users.get(probe) or _lease_reason(held, target)
        if users is not None and reason is None:
            idle[path] = target
            continue
        if reason is not None:
            LOG.info("kept %s %s (second look): %s", kind, path, reason)
        _put_back(path, target, kind=kind, keep=keep)
        del pending[path]
    return idle


def _lease_reason(held: Callable[[Path], bool] | None, target: Path) -> str | None:
    if held is None:
        return None
    try:
        return "a lease was taken on it" if held(target) else None
    except Exception:  # unreadable leases are an owner we cannot rule out
        LOG.debug("lease check of %s failed", target, exc_info=True)
        return "its leases cannot be read"


def _put_back(path: Path, target: Path, *, kind: str, keep: Collection[str] | None) -> None:
    """Undo :func:`_move_aside`. What cannot be moved back stays where it is, named in a warning:
    still in use it is found at its new name by the next pass, and never removed in the meantime."""
    if not os.path.lexists(target):
        return
    try:
        if keep is None:
            if os.path.lexists(path):  # the rename never happened: that is not ours to move onto it
                LOG.warning("%s %s: %s is not its set-aside copy (the name is in use); left alone", kind, path, target)
                return
            os.rename(target, path)
            return
        for child in sorted(target.iterdir()):
            try:
                os.rename(child, path / child.name)
            except OSError:
                LOG.warning("%s %s: %s stays set aside in %s; could not put it back", kind, path, child.name,
                            target, exc_info=True)
        with contextlib.suppress(OSError):
            target.rmdir()
    except OSError:
        LOG.warning("%s %s stays set aside as %s; could not put it back", kind, path, target, exc_info=True)


def users_of(candidates: Collection[Path], *, own_files: Collection[Path] = ()) -> dict[Path, str] | None:
    """Why each candidate directory is in use; a candidate that is not is absent.

    In use: a live process's executable, working directory, open or mapped files, argv or
    environment name a path inside it, or one of this account's launchd agents or systemd user
    units names it (program, arguments, ``PATH``; loaded or not, since it can start at any time).
    None when the processes cannot be listed.

    ``own_files`` are files this very process holds open inside a candidate on purpose (the
    install lock a collector keeps while it works): that one open file is not a use. Everything
    else this process does still counts, a thread of it that began using a candidate included.
    """
    spellings = _spellings(candidates)
    users = _process_users(spellings, ignore=frozenset(_spellings(own_files)))
    if users is not None:
        for path, reason in _unit_users(spellings).items():
            users.setdefault(path, reason)
    return users


def _original(name: str) -> str:
    """The name a generation had before a collector set it aside (``<name>.collecting-<hex>``)."""
    return name.partition(ASIDE_MARK)[0]


def _spellings(candidates: Collection[Path]) -> dict[bytes, list[Path]]:
    """Each candidate as given and as the kernel reports it (``/tmp`` is ``/private/tmp`` on macOS),
    mapped to every candidate it names.

    A generation left set aside (``<name>.collecting-<hex>``, by a collector that was killed or
    could not put it back) also answers to the name it had: a process that started from it before
    names that path in its argv and environment, and a service definition names it too. Several
    leftovers of one generation share that name, and each of them answers to it."""
    named = [(path, path) for path in candidates]
    named += [(path.with_name(_original(path.name)), path)
              for path in candidates if ASIDE_MARK in path.name and _original(path.name)]
    spellings: dict[bytes, list[Path]] = {}
    for where, path in named:
        for spelling in (os.path.abspath(where), os.path.realpath(where)):
            owners = spellings.setdefault(os.fsencode(os.path.normcase(spelling)), [])
            if path not in owners:
                owners.append(path)
    return spellings


def _matches(spellings: dict[bytes, list[Path]], users: dict[Path, str], blob: bytes) -> list[Path]:
    """Candidates not yet in use that ``blob`` names. Plain containment is exact here: generations,
    PM runtime generations and install states are all fixed-width ids, none a prefix of another
    (the leftovers of one generation, which share a prefix, are all kept together)."""
    found: list[Path] = []
    for spelling, paths in spellings.items():
        if spelling in blob:
            found += [path for path in paths if path not in users and path not in found]
    return found


def _process_users(spellings: dict[bytes, list[Path]], *, ignore: frozenset[bytes] = frozenset()) -> dict[Path, str] | None:
    if os.path.isdir("/proc/self/fd"):
        return _proc_users(spellings, ignore=ignore)
    if os.name == "posix":
        return _listing_users(spellings, ignore=ignore)
    return _psutil_users(spellings, ignore=ignore)


def _without(blob: bytes, ignore: frozenset[bytes]) -> bytes:
    """``blob`` (``\\0``-joined file names) minus the names in ``ignore``."""
    return b"\0".join(name for name in blob.split(b"\0") if name not in ignore) if ignore else blob


def _proc_users(spellings: dict[bytes, list[Path]], *, ignore: frozenset[bytes] = frozenset()) -> dict[Path, str] | None:
    """Linux: what ``/proc`` shows of every process.

    Another user's process shows only its argv; one that exits mid-scan shows nothing more. The
    scan counts only if it saw this process's own argv: a ``/proc`` that hides even that is no
    listing at all. ``ignore`` names files this process alone may hold open without that being a use.
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
            if pid == own and what == "open file":
                blob = _without(blob, ignore)
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


def _listing_users(spellings: dict[bytes, list[Path]], *,
                   ignore: frozenset[bytes] = frozenset()) -> dict[Path, str] | None:
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
    if _scan_lsof(files, spellings, users, own, ignore) and _scan_ps(table, spellings, users, own):
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


def _scan_lsof(listing: bytes, spellings: dict[bytes, list[Path]], users: dict[Path, str], own: str,
               ignore: frozenset[bytes] = frozenset()) -> bool:
    """Record what ``lsof`` names; True when the listing includes ``own`` (it always lists a cwd).
    ``own`` may hold the files in ``ignore`` open without that being a use."""
    witnessed = False
    for pid, command, fd, name in _lsof_files(listing):
        witnessed = witnessed or pid == own
        if pid == own and fd.isdigit() and name in ignore:  # the open descriptor, not a mapping of it
            continue
        for path in _matches(spellings, users, name):
            users[path] = f"pid {pid} ({command}): file, fd {fd}"
    return witnessed


def _scan_ps(table: bytes, spellings: dict[bytes, list[Path]], users: dict[Path, str], own: str) -> bool:
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


def _psutil_users(spellings: dict[bytes, list[Path]], *, ignore: frozenset[bytes] = frozenset()) -> dict[Path, str] | None:
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
            if process.pid == os.getpid() and what == "open file":
                blob = _without(blob, ignore)
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


def _unit_users(spellings: dict[bytes, list[Path]]) -> dict[Path, str]:
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
