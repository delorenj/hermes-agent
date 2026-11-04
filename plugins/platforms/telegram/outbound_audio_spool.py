"""Disabled-by-default local spool of audio a Telegram bot actually delivered to an allowlisted DM.

Inert unless ``extra.outbound_audio_spool.enabled`` is true. No networking: a later consumer drains
``pending()`` entries before ``expires_at``. Admission requires positive provenance on the send's
metadata (``outbound_audio_provenance == "native_reply"``); no caller sets it yet, so even an enabled
spool captures nothing until provenance wiring lands.

Source-stability guarantee (what "the bytes Telegram delivered" means here): before the send, a worker
thread snapshots the source through a private ``dup`` of its descriptor and records (dev, inode, size,
mtime_ns, ctime_ns) for both the descriptor and, when the handle names a path, the path. After a
successful send it re-reads and re-stats both; any identity/metadata difference, path replacement or
byte difference rejects the capture (the send result is untouched). This detects mutations that persist
across the send window. It is NOT byte-exact against a writer that changes the file and restores it
within the window, nor does it prove what the transport buffered: it proves the published bytes equal the
source both immediately before and after a successful send.

Concurrency/filesystem assumptions: a local POSIX filesystem with working ``flock``, ``O_NOFOLLOW``,
``link`` (fails with EEXIST, never overwrites) and ``fsync``. Every mutation (recovery, prune, admission
to publication) and every ``pending()`` read happens under one exclusive advisory lock on ``<root>/.lock``,
shared by independent processes. Network filesystems are unsupported.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # non-POSIX host: no flock, so the spool is unavailable (fail closed)
    fcntl = None

logger = logging.getLogger(__name__)

PROVENANCE_KEY = "outbound_audio_provenance"
ADMITTED_PROVENANCE = "native_reply"
# Mirrors the Earshot consumer contract (host/earshot.py AUDIO_EXTS, AudioKeys.extensions) exactly.
AUDIO_EXTENSIONS = frozenset({".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".webm"})
MAX_TTL_SECONDS = 1799  # strictly under 30 minutes from the original Telegram send
FUTURE_SKEW_SECONDS = 60
ORPHAN_GRACE_SECONDS = 60
MAX_META_BYTES = 4096
LOCK_NAME = ".lock"
_MEDIA_KEYS = {"voice": "voice", "audio": "audio", "document": "document"}
_KEY_RE = re.compile(r"^([1-9][0-9]{0,19})_([1-9][0-9]{0,19})_([1-9][0-9]{0,19})_0$")
_META_FIELDS = frozenset({"bot_id", "chat_id", "message_id", "attachment_index", "media_kind", "extension",
                          "sent_at", "expires_at", "bytes", "sha256", "audio_file"})
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class _Invalid(Exception):
    """Metadata or audio failed validation; the entry is never trusted."""


@dataclass(frozen=True)
class SpoolSettings:
    enabled: bool
    root: Path
    allowed_chat_ids: frozenset[int]
    ttl_seconds: int = 900
    max_entries: int = 200
    max_bytes: int = 200 * 1024 * 1024

    @classmethod
    def from_extra(cls, extra: Optional[dict[str, Any]], hermes_home: Path) -> "SpoolSettings":
        raw = (extra or {}).get("outbound_audio_spool") or {}
        if not isinstance(raw, dict):
            raw = {}
        ttl = min(max(int(raw.get("ttl_seconds", 900)), 1), MAX_TTL_SECONDS)
        root = Path(raw["dir"]).expanduser() if raw.get("dir") else hermes_home / "spool" / "outbound_audio"
        return cls(
            enabled=raw.get("enabled") is True,
            root=root,
            allowed_chat_ids=frozenset(int(c) for c in raw.get("allowed_chat_ids") or () if int(c) > 0),
            ttl_seconds=ttl,
            max_entries=max(int(raw.get("max_entries", 200)), 1),
            max_bytes=max(int(raw.get("max_bytes", 200 * 1024 * 1024)), 1),
        )


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    """Finite real usable as a float; JSON ints too large for a float (``10**309``) are not numbers here."""
    if isinstance(v, float):
        return math.isfinite(v)
    if not _is_int(v):
        return False
    try:
        float(v)
    except OverflowError:
        return False
    return True


def _media_ext(media_label: str, media_kwargs: dict[str, Any], fh: Any) -> Optional[str]:
    name = media_kwargs.get("filename") or os.path.basename(str(getattr(fh, "name", "")))
    ext = os.path.splitext(name)[1].lower()
    if media_label == "document" and ext not in AUDIO_EXTENSIONS:
        return None
    return ext if ext in AUDIO_EXTENSIONS else ""


class OutboundAudioSpool:
    def __init__(self, settings: SpoolSettings, clock=time.time):
        self.settings = settings
        self._clock = clock

    # ── admission ────────────────────────────────────────────────────────────
    def eligible(self, metadata: Optional[dict[str, Any]], media_label: str,
                 media_kwargs: dict[str, Any]) -> Optional[tuple[Any, str]]:
        """Pre-send gate (no I/O): ``(handle, extension)`` when this send may be captured."""
        if not self.settings.enabled or media_label not in _MEDIA_KEYS:
            return None
        if (metadata or {}).get(PROVENANCE_KEY) != ADMITTED_PROVENANCE:
            return None
        fh = media_kwargs.get(_MEDIA_KEYS[media_label])
        if not hasattr(fh, "read"):
            return None
        ext = _media_ext(media_label, media_kwargs, fh)
        return None if ext is None else (fh, ext)

    def admit(self, *, requested_chat_id: Any, media_label: str, extension: str, message: Any,
              adapter_bot_id: Optional[int]) -> Optional[dict[str, Any]]:
        """Post-send gate: the entry record for an admissible delivered message, else ``None``."""
        s = self.settings
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if (getattr(chat, "type", None) != "private" or not _is_int(chat_id)
                or str(chat_id) != str(requested_chat_id).strip() or chat_id not in s.allowed_chat_ids):
            return None
        sender = getattr(message, "from_user", None)
        bot_id = getattr(sender, "id", None) if getattr(sender, "is_bot", False) is True else None
        if not _is_int(bot_id):
            bot_id = adapter_bot_id
        elif adapter_bot_id is not None and adapter_bot_id != bot_id:
            return None
        message_id = getattr(message, "message_id", None)
        sent = getattr(message, "date", None)
        if not _is_int(bot_id) or bot_id <= 0 or not _is_int(message_id) or message_id <= 0:
            return None
        if not isinstance(sent, datetime) or sent.tzinfo is None:
            return None
        sent_at = sent.timestamp()
        now = self._clock()
        if sent_at > now + FUTURE_SKEW_SECONDS or now - sent_at >= s.ttl_seconds:
            return None
        return {"bot_id": bot_id, "chat_id": chat_id, "message_id": message_id, "attachment_index": 0,
                "media_kind": media_label, "extension": extension,
                "sent_at": sent_at, "expires_at": sent_at + s.ttl_seconds}

    # ── persistence ──────────────────────────────────────────────────────────
    @staticmethod
    def entry_key(record: dict[str, Any]) -> str:
        return "{bot_id}_{chat_id}_{message_id}_{attachment_index}".format(**record)

    def store(self, record: dict[str, Any], data: bytes) -> bool:
        """Persist delivered bytes + metadata; never overwrite or remove another writer's entry.

        ``record`` is not mutated. Admission through publication runs under the cross-process lock.
        """
        if not data:
            return False
        key = self.entry_key(record)
        if not _KEY_RE.match(key):
            return False
        with self._locked() as root:
            now = self._clock()
            entries = self._recover(root, now)
            meta_name, audio_name = f"{key}.json", f"{key}.audio"
            if _lexists(root / meta_name):
                return False
            # No commit marker under the lock means no live writer owns this name: crash leftover.
            _unlink_entry(root / audio_name)
            used = sum(m["bytes"] for m in entries)
            if len(entries) + 1 > self.settings.max_entries or used + len(data) > self.settings.max_bytes:
                return False
            meta = {**record, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "audio_file": audio_name}
            try:
                self._validate(meta, meta_name, now)
            except _Invalid:
                return False
            if not _publish(root / audio_name, data):
                return False
            try:
                published = _publish(root / meta_name, json.dumps(meta, sort_keys=True).encode())
            except BaseException:
                _unlink_entry(root / audio_name)  # created by us, under the lock
                raise
            if not published:
                _unlink_entry(root / audio_name)
                return False
            _fsync_dir(root)
            return True

    def pending(self) -> list[dict[str, Any]]:
        """Unexpired, fully validated committed entries, oldest send first. Expiry is from the original send."""
        if not _lexists(self.settings.root):
            return []
        with self._locked() as root:
            entries = self._recover(root, self._clock())
        return sorted(entries, key=lambda m: m["sent_at"])

    def read_for_drain(self, meta: Any) -> Optional[bytes]:
        """Audio bytes of a ``pending()`` entry, re-validated and read under the lock.

        ``None`` when the entry is gone, changed in any field, expired, or its audio no longer hashes to
        ``sha256``. The caller does network I/O with the returned bytes outside the lock.
        """
        key = _entry_key_of(meta)
        if key is None or not _lexists(self.settings.root):
            return None
        with self._locked() as root:
            now = self._clock()
            current = self._current(root, key, meta, now)
            if current is None or current["expires_at"] <= now:
                return None
            try:
                return _verify_audio(root / current["audio_file"], current)
            except _Invalid:
                return None

    def consume(self, meta: Any, expected_sha256: str) -> bool:
        """Remove exactly the entry ``meta`` names, only if it is unchanged and its audio still hashes to
        ``expected_sha256`` and has not expired (checked under the lock). Paths derive from the validated
        key, never from the caller."""
        key = _entry_key_of(meta)
        if key is None or not _lexists(self.settings.root):
            return False
        with self._locked() as root:
            now = self._clock()
            current = self._current(root, key, meta, now)
            if current is None or current["sha256"] != expected_sha256 or current["expires_at"] <= now:
                return False
            try:
                _verify_audio(root / current["audio_file"], current)
            except _Invalid:
                return False
            _unlink_entry(root / f"{key}.json")  # commit marker first: a crash leaves only an orphan
            _unlink_entry(root / current["audio_file"])
            _fsync_dir(root)
            return True

    def prune(self) -> None:
        """Remove expired/invalid commit markers, stale partial writes and orphaned audio."""
        if not _lexists(self.settings.root):
            return
        with self._locked() as root:
            self._recover(root, self._clock())

    # ── recovery (caller holds the lock) ─────────────────────────────────────
    def _recover(self, root: Path, now: float) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        names = os.listdir(root)
        for name in names:
            if not name.endswith(".json"):
                continue
            path = root / name
            try:
                meta = self._load_meta(path, now)
                _verify_audio(root / meta["audio_file"], meta)
            except _Invalid:
                _unlink_entry(path)  # only this directory entry; its contents are never trusted
                continue
            if meta["expires_at"] <= now:
                _unlink_entry(root / meta["audio_file"])  # canonical name derived from the validated key
                _unlink_entry(path)
                continue
            valid.append(meta)
        committed = {m["audio_file"] for m in valid}
        for name in names:
            orphan = name.endswith(".audio") and name not in committed
            if not (orphan or name.startswith(".tmp-")):
                continue
            path = root / name
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode) or now - st.st_mtime <= ORPHAN_GRACE_SECONDS:
                continue
            _unlink_entry(path)
        return valid

    def _current(self, root: Path, key: str, meta: dict[str, Any], now: float) -> Optional[dict[str, Any]]:
        """The on-disk entry for ``key`` when it validates and equals ``meta`` field for field."""
        try:
            current = self._load_meta(root / f"{key}.json", now)
        except _Invalid:
            return None
        return current if current == meta else None

    def _load_meta(self, path: Path, now: float) -> dict[str, Any]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError as e:
            raise _Invalid(str(e)) from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_META_BYTES:
                raise _Invalid("metadata is not a small regular file")
            raw = os.read(fd, MAX_META_BYTES + 1)
        finally:
            os.close(fd)
        try:
            meta = json.loads(raw, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError) as e:
            raise _Invalid("malformed metadata") from e
        self._validate(meta, path.name, now)
        return meta

    def _validate(self, meta: Any, meta_name: str, now: float) -> None:
        if not isinstance(meta, dict) or set(meta) != _META_FIELDS:
            raise _Invalid("schema")
        for k in ("bot_id", "chat_id", "message_id", "attachment_index", "bytes"):
            if not _is_int(meta[k]):
                raise _Invalid(k)
        if meta["attachment_index"] != 0 or meta["bytes"] <= 0 or meta["bytes"] > self.settings.max_bytes:
            raise _Invalid("scalar range")
        key = self.entry_key(meta)
        if not _KEY_RE.match(key) or meta_name != f"{key}.json" or meta["audio_file"] != f"{key}.audio":
            raise _Invalid("identity")
        if not isinstance(meta["media_kind"], str) or not isinstance(meta["extension"], str):
            raise _Invalid("media type")
        if meta["media_kind"] not in _MEDIA_KEYS:
            raise _Invalid("media")
        if meta["extension"] not in AUDIO_EXTENSIONS and meta["extension"] != "":
            raise _Invalid("extension")
        if meta["media_kind"] == "document" and meta["extension"] == "":
            raise _Invalid("document extension")
        if not isinstance(meta["sha256"], str) or not _SHA_RE.match(meta["sha256"]):
            raise _Invalid("digest")
        sent, exp = meta["sent_at"], meta["expires_at"]
        if not (_is_num(sent) and _is_num(exp)) or sent <= 0:
            raise _Invalid("timestamps")
        if not 0 < exp - sent <= MAX_TTL_SECONDS or sent > now + FUTURE_SKEW_SECONDS:
            raise _Invalid("ttl")

    # ── lock ─────────────────────────────────────────────────────────────────
    @contextmanager
    def _locked(self) -> Iterator[Path]:
        if fcntl is None:
            raise OSError(errno.ENOSYS, "outbound audio spool requires POSIX flock")
        root = self.settings.root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(root)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise OSError(errno.ENOTDIR, "spool root must be a real directory owned by this user", str(root))
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(root, 0o700)
        fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        try:
            lst = os.fstat(fd)
            if not stat.S_ISREG(lst.st_mode) or lst.st_uid != os.geteuid():
                raise OSError(errno.EPERM, "spool lock must be a regular file owned by this user", str(root))
            if stat.S_IMODE(lst.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield root
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _entry_key_of(meta: Any) -> Optional[str]:
    """Canonical key of a caller-supplied record, or ``None`` when it cannot name a spool entry."""
    if not isinstance(meta, dict) or set(meta) != _META_FIELDS:
        return None
    key = OutboundAudioSpool.entry_key(meta)
    return key if _KEY_RE.match(key) else None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _unlink_entry(path: Path) -> None:
    """Remove one directory entry (never follows symlinks); directories are left alone."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            os.unlink(path)
    except FileNotFoundError:
        pass


def _verify_audio(path: Path, meta: dict[str, Any]) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as e:
        raise _Invalid("audio missing or not a plain file") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != meta["bytes"]:
            raise _Invalid("audio kind/size")
        data = _read_all(fd, st.st_size)
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise _Invalid("audio corrupt")
        return data
    finally:
        os.close(fd)


def _read_all(fd: int, size: int) -> bytes:
    """Positional read of exactly ``size`` bytes (shorter only if the file shrank)."""
    chunks, off = [], 0
    while off < size:
        chunk = os.pread(fd, min(size - off, 1 << 20), off)
        if not chunk:
            break
        chunks.append(chunk)
        off += len(chunk)
    return b"".join(chunks)


def _publish(final: Path, data: bytes) -> bool:
    tmp = final.parent / f".tmp-{final.name}-{os.getpid()}-{time.monotonic_ns()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, final)  # local POSIX: EEXIST instead of overwrite
        except FileExistsError:
            return False
        return True
    finally:
        _unlink_entry(tmp)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ── source snapshot (runs in a worker thread; owns a private dup of the descriptor) ──

def _fingerprint(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class _Source:
    """Snapshot of a send's media taken before the send; ``verify()`` re-checks it afterwards."""

    def __init__(self, fd: Optional[int], path: Optional[str], memory: Any):
        self._fd, self._path, self._memory = fd, path, memory
        self.data: bytes = b""
        self._fp: Optional[tuple] = None
        self._path_fp: Optional[tuple] = None

    @classmethod
    def borrow(cls, fh: Any) -> "_Source":
        """Event-loop side, no blocking I/O: dup the descriptor so later thread work never touches ``fh``."""
        try:
            fileno = fh.fileno()
        except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is an OSError
            fileno = None
        if fileno is None:
            return cls(None, None, fh)
        name = getattr(fh, "name", None)
        return cls(os.dup(fileno), name if isinstance(name, str) else None, None)

    def _stat_pair(self) -> tuple[tuple, Optional[tuple]]:
        fp = _fingerprint(os.fstat(self._fd))
        path_fp = _fingerprint(os.stat(self._path)) if self._path else None
        return fp, path_fp

    def snapshot(self, limit: int) -> bool:
        if self._fd is None:
            self.data = bytes(self._memory.getvalue()) if hasattr(self._memory, "getvalue") else b""
            return 0 < len(self.data) <= limit
        self._fp, self._path_fp = self._stat_pair()
        if self._path_fp is not None and self._path_fp != self._fp:
            return False
        if not stat.S_ISREG(os.fstat(self._fd).st_mode) or not 0 < self._fp[2] <= limit:
            return False
        self.data = _read_all(self._fd, self._fp[2])
        return len(self.data) == self._fp[2] and self._stat_pair() == (self._fp, self._path_fp)

    def verify(self) -> bool:
        if self._fd is None:
            return hasattr(self._memory, "getvalue") and bytes(self._memory.getvalue()) == self.data
        if self._stat_pair() != (self._fp, self._path_fp):
            return False
        if _read_all(self._fd, self._fp[2] + 1) != self.data:
            return False
        return self._stat_pair() == (self._fp, self._path_fp)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


async def _run_drained(fn, *args) -> Any:
    """Run ``fn`` in a thread; on cancellation wait for it to finish before re-raising (no orphan worker)."""
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        while not fut.done():
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        raise


class AudioCapture:
    """One send's capture lifecycle: ``prepare`` before the send, ``finish`` after success, ``close`` always."""

    def __init__(self, spool: Optional[OutboundAudioSpool] = None, source: Optional[_Source] = None,
                 extension: str = "", media_label: str = ""):
        self._spool, self._source, self._ext, self._label = spool, source, extension, media_label

    async def finish(self, bot: Any, chat_id: Any, message: Any) -> None:
        """Best effort after a successful send; only cancellation propagates, after the worker drains."""
        if self._spool is None or self._source is None:
            return
        try:
            try:
                adapter_bot_id = bot.id
            except Exception:  # PTB raises RuntimeError before initialize(); message sender still identifies the bot
                adapter_bot_id = None
            record = self._spool.admit(
                requested_chat_id=chat_id, media_label=self._label, extension=self._ext, message=message,
                adapter_bot_id=adapter_bot_id if _is_int(adapter_bot_id) else None)
            if record is not None:
                await _run_drained(self._finish_sync, record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("outbound audio spool capture failed; Telegram send unaffected", exc_info=True)

    def _finish_sync(self, record: dict[str, Any]) -> bool:
        if not self._source.verify():
            logger.warning("outbound audio source changed across the send; capture rejected")
            return False
        return self._spool.store(record, self._source.data)

    def close(self) -> None:
        if self._source is not None:
            self._source.close()
            self._source = None


async def prepare_capture(spool: Optional[OutboundAudioSpool], metadata: Optional[dict[str, Any]],
                          media_label: str, media_kwargs: dict[str, Any]) -> AudioCapture:
    """Pre-send snapshot; any failure yields an inert capture. Never raises except on cancellation."""
    if spool is None:
        return AudioCapture()
    source = None
    try:
        gate = spool.eligible(metadata, media_label, media_kwargs)
        if gate is None:
            return AudioCapture()
        fh, ext = gate
        source = _Source.borrow(fh)
        if await _run_drained(source.snapshot, spool.settings.max_bytes):
            return AudioCapture(spool, source, ext, media_label)
    except asyncio.CancelledError:
        if source is not None:
            source.close()
        raise
    except Exception:
        logger.warning("outbound audio spool snapshot failed; Telegram send unaffected", exc_info=True)
    if source is not None:
        source.close()
    return AudioCapture()


def build_spool(extra: Optional[dict[str, Any]], hermes_home: Path) -> Optional[OutboundAudioSpool]:
    """``None`` when disabled or misconfigured: no spool object, no filesystem access."""
    try:
        settings = SpoolSettings.from_extra(extra, hermes_home)
    except (TypeError, ValueError, KeyError):
        logger.warning("outbound_audio_spool config invalid; spool stays disabled")
        return None
    if settings.enabled and fcntl is None:
        logger.warning("outbound_audio_spool needs POSIX flock, unavailable on this host; spool stays disabled")
        return None
    return OutboundAudioSpool(settings) if settings.enabled else None
