"""Disabled-by-default private uploader that drains the outbound audio spool to a private S3 bucket.

Inert unless ``extra.outbound_audio_drain.enabled`` is literally ``true``: otherwise no client, no
uploader object and no filesystem access exist. The only accepted origin is ``https://s3.delo.sh`` and
the only accepted bucket is the private ``earshot-replies`` (the public ``earshot`` bucket is refused by
name). Credentials never come from config: the real client is built only from the dedicated pair
``HOLOC_OUTBOUND_AUDIO_S3_ACCESS_KEY_ID`` / ``HOLOC_OUTBOUND_AUDIO_S3_SECRET_ACCESS_KEY`` in the process
environment. Missing or placeholder values disable the drain; the botocore default chain (``AWS_*`` env,
``~/.aws`` files, instance metadata) is never consulted and there is no unsigned/anonymous fallback.

Security/consistency contract of one ``drain_once()`` pass (foreground, bounded, no threads/loops):

* Writes are conditional creates (``If-None-Match: *``) to a deterministic key derived from the entry's
  validated identity, so a retry targets the same object and an existing object is never overwritten.
* The bytes sent are the bytes re-validated (identity + sha256) under the spool lock; the network call
  runs outside that lock. A local entry is removed only through ``OutboundAudioSpool.consume``, which
  re-validates identity and hash under the lock, and only once the remote object is proven to hold
  exactly this entry (success with an ETag, or a HEAD whose size and sha256/bot/chat/message/sent_at
  metadata all match, plus ``expires_at`` when the object carries it).
* An ambiguous outcome (timeout, dropped connection, 5xx) is resolved by HEAD, never by rewriting: no
  proof of a matching object means the entry stays queued for the next pass.
* Expiry is measured from the original Telegram send (``expires_at``). Entries already expired when the
  pass starts are pruned by ``pending()`` recovery and never reach the network. Expiry is re-checked
  before reading, before the remote call, after it, and again under the spool lock inside ``consume``:
  an entry that expires mid-pass is counted ``expired``, never acknowledged, and left for the spool's
  recovery to prune (an object may already exist remotely in that case).
* An auth failure ends the pass: the refused entry and every later entry stay queued (counted
  ``failed`` / ``kept``); entries consumed earlier in the same pass stay consumed. Any other unexpected
  SDK error propagates out of the pass without removing the entry in flight. Concurrent passes serialize on
  ``<root>/.drain.lock``, separate from the spool lock, so the spool is never locked across network I/O.
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from plugins.platforms.telegram.outbound_audio_spool import (
    OutboundAudioSpool,
    SpoolSettings,
    _is_num,
    fcntl,
)

try:
    import botocore.session
    from botocore.config import Config
    from botocore.exceptions import (
        ClientError,
        ConnectionError as BotoConnectionError,
        HTTPClientError,
        NoCredentialsError,
        PartialCredentialsError,
    )
except ImportError:  # optional dependency: without botocore the drain is unavailable (fail closed)
    botocore = None

logger = logging.getLogger(__name__)

ALLOWED_ENDPOINT = "https://s3.delo.sh"
PRIVATE_BUCKET = "earshot-replies"
PUBLIC_BUCKET = "earshot"
DRAIN_LOCK_NAME = ".drain.lock"
ACCESS_KEY_ENV = "HOLOC_OUTBOUND_AUDIO_S3_ACCESS_KEY_ID"
SECRET_KEY_ENV = "HOLOC_OUTBOUND_AUDIO_S3_SECRET_ACCESS_KEY"
_PLACEHOLDER_RE = re.compile(r"(?i)^(?:op://.*|.*changeme.*|.*placeholder.*|.*replace.?me.*|your.*|x+|\*+|<.*>|\$\{.*\}|none|null|todo)$")
MAX_TIMEOUT_SECONDS = 120
_PREFIX_RE = re.compile(r"^(?:[A-Za-z0-9_-]+/)*$")
_REGION_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_AUTH_CODES = frozenset({"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken",
                         "InvalidToken", "AccountProblem", "AllAccessDisabled"})
_ABSENT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_IDENTITY_FIELDS = ("bot_id", "chat_id", "message_id")
_MATCH_FIELDS = ("sha256", "sent_at", *_IDENTITY_FIELDS)


@dataclass(frozen=True)
class DrainSettings:
    enabled: bool = False
    endpoint_url: str = ALLOWED_ENDPOINT
    bucket: str = PRIVATE_BUCKET
    prefix: str = "replies/"
    region: str = "us-east-1"
    connect_timeout: float = 5
    read_timeout: float = 15

    @classmethod
    def from_extra(cls, extra: Optional[dict[str, Any]], hermes_home: Path) -> "DrainSettings":
        """Settings from ``extra.outbound_audio_drain``; any unsafe or malformed value disables the drain.

        ``hermes_home`` mirrors ``SpoolSettings.from_extra``; the drain derives no paths of its own.
        """
        raw = (extra or {}).get("outbound_audio_drain") or {}
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return cls()
        default = cls()
        values = {k: raw.get(k, getattr(default, k)) for k in
                  ("endpoint_url", "bucket", "prefix", "region", "connect_timeout", "read_timeout")}
        problem = _settings_problem(**values)
        if problem:
            logger.warning("outbound_audio_drain %s; drain stays disabled", problem)
            return cls()
        return cls(enabled=True, **values)


def _settings_problem(*, endpoint_url: Any, bucket: Any, prefix: Any, region: Any,
                      connect_timeout: Any, read_timeout: Any) -> Optional[str]:
    if endpoint_url != ALLOWED_ENDPOINT:
        return f"endpoint_url must be exactly {ALLOWED_ENDPOINT}"
    if bucket == PUBLIC_BUCKET:
        return "refuses the public earshot bucket"
    if bucket != PRIVATE_BUCKET:
        return f"bucket must be exactly {PRIVATE_BUCKET}"
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        return "prefix must be empty or '/'-terminated plain path segments"
    if not isinstance(region, str) or not _REGION_RE.match(region):
        return "region is invalid"
    for t in (connect_timeout, read_timeout):
        if not _is_num(t) or not 0 < t <= MAX_TIMEOUT_SECONDS:
            return f"timeouts must be in (0, {MAX_TIMEOUT_SECONDS}] seconds"
    return None


def _usable(settings: DrainSettings) -> bool:
    return settings.enabled and _settings_problem(
        endpoint_url=settings.endpoint_url, bucket=settings.bucket, prefix=settings.prefix,
        region=settings.region, connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout) is None


@dataclass
class DrainResult:
    uploaded: int = 0
    kept: int = 0
    expired: int = 0
    failed: int = 0
    collided: int = 0


class _AuthFailure(Exception):
    """Credentials were refused; the rest of the pass would fail the same way."""


def _credential(environ: Any, name: str) -> Optional[str]:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    return None if _PLACEHOLDER_RE.match(value) else value


def _new_client(settings: DrainSettings, environ: Any = None) -> Any:
    """Private S3 client from the dedicated credential pair only; ``None`` when it is missing/placeholder.

    Signed s3v4, short timeouts, no SDK retries. Shared config/credential files are pointed at
    ``os.devnull`` so no ``~/.aws`` profile, chain provider or instance metadata can contribute.
    """
    environ = os.environ if environ is None else environ
    key_id, secret = _credential(environ, ACCESS_KEY_ENV), _credential(environ, SECRET_KEY_ENV)
    if key_id is None or secret is None:
        logger.warning("outbound_audio_drain needs %s and %s; drain stays disabled", ACCESS_KEY_ENV, SECRET_KEY_ENV)
        return None
    session = botocore.session.Session()
    session.set_config_variable("config_file", os.devnull)
    session.set_config_variable("credentials_file", os.devnull)
    return session.create_client(
        "s3", endpoint_url=settings.endpoint_url, region_name=settings.region,
        aws_access_key_id=key_id, aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", connect_timeout=settings.connect_timeout,
                      read_timeout=settings.read_timeout, retries={"max_attempts": 0}))


def remote_key(prefix: str, meta: dict[str, Any]) -> str:
    """Deterministic object key: same identity → same key on every retry."""
    return "{p}{bot_id}/{chat_id}/{message_id}/{attachment_index}{extension}".format(p=prefix, **meta)


def _object_metadata(meta: dict[str, Any]) -> dict[str, str]:
    """``expires_at`` (original send + TTL, epoch seconds) lets the phone enforce the same hard expiry."""
    return {"sha256": meta["sha256"], "sent_at": str(meta["sent_at"]), "expires_at": str(meta["expires_at"]),
            **{k: str(meta[k]) for k in _IDENTITY_FIELDS}}


def _error_code(e: ClientError) -> tuple[str, int]:
    resp = getattr(e, "response", None) or {}
    return str((resp.get("Error") or {}).get("Code", "")), int(
        (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)


class PrivateAudioDrain:
    def __init__(self, spool: OutboundAudioSpool, settings: DrainSettings, s3_client: Any = None,
                 clock=time.time, environ: Any = None):
        self.spool = spool
        self.settings = settings
        self._clock = clock
        self._client = None
        if _usable(settings) and botocore is not None and fcntl is not None:
            self._client = s3_client if s3_client is not None else _new_client(settings, environ)

    # ── pass ─────────────────────────────────────────────────────────────────
    def drain_once(self) -> DrainResult:
        """One bounded foreground pass over ``pending()``, oldest send first."""
        result = DrainResult()
        if self._client is None or not os.path.lexists(self.spool.settings.root):
            return result
        with self._drain_locked():
            entries = self.spool.pending()
            for i, meta in enumerate(entries):
                try:
                    self._drain_entry(meta, result)
                except _AuthFailure:
                    result.failed += 1
                    result.kept += len(entries) - i - 1
                    logger.warning("outbound audio drain: credentials refused; pass ended, %d entries left queued",
                                   len(entries) - i)
                    break
        return result

    def _expired(self, meta: dict[str, Any]) -> bool:
        return self._clock() >= meta["expires_at"]

    def _drain_entry(self, meta: dict[str, Any], result: DrainResult) -> None:
        if self._expired(meta):
            result.expired += 1
            return
        data = self.spool.read_for_drain(meta)
        if data is None:
            if self._expired(meta):
                result.expired += 1
            else:
                result.kept += 1
            return
        if self._expired(meta):
            result.expired += 1
            return
        outcome = self._put(remote_key(self.settings.prefix, meta), meta, data)
        if outcome == "stored":
            self._finish(meta, result)
        elif outcome == "collided":
            result.collided += 1
            logger.warning("outbound audio drain: remote object for %s holds different audio; kept",
                           self.spool.entry_key(meta))
        elif outcome == "failed":
            result.failed += 1
        else:
            result.kept += 1

    def _put(self, key: str, meta: dict[str, Any], data: bytes) -> str:
        """``stored`` | ``collided`` | ``failed`` | ``kept``; raises ``_AuthFailure`` on refused credentials."""
        try:
            resp = self._client.put_object(Bucket=self.settings.bucket, Key=key, Body=data, IfNoneMatch="*",
                                           Metadata=_object_metadata(meta))
        except ClientError as e:
            code, status = _error_code(e)
            if status == 412 or code == "PreconditionFailed":
                return {"match": "stored", "mismatch": "collided"}.get(self._remote_state(key, meta), "kept")
            if status in (401, 403) or code in _AUTH_CODES:
                raise _AuthFailure from e
            if status >= 500 or code in ("RequestTimeout", "InternalError", "SlowDown", "ServiceUnavailable"):
                return self._resolve_ambiguous(key, meta)
            logger.warning("outbound audio drain: put rejected (%s %s); kept", status, code)
            return "failed"
        except (NoCredentialsError, PartialCredentialsError) as e:
            raise _AuthFailure from e
        except (HTTPClientError, BotoConnectionError):
            return self._resolve_ambiguous(key, meta)
        if not resp.get("ETag"):
            return self._resolve_ambiguous(key, meta)
        return "stored"

    def _resolve_ambiguous(self, key: str, meta: dict[str, Any]) -> str:
        """The write may or may not have landed: only a matching HEAD proves it did."""
        return "stored" if self._remote_state(key, meta) == "match" else "kept"

    def _remote_state(self, key: str, meta: dict[str, Any]) -> str:
        """``match`` | ``mismatch`` | ``absent`` | ``unknown`` for the object at ``key``."""
        try:
            head = self._client.head_object(Bucket=self.settings.bucket, Key=key)
        except ClientError as e:
            code, status = _error_code(e)
            if status == 404 or code in _ABSENT_CODES:
                return "absent"
            if status in (401, 403) or code in _AUTH_CODES:
                raise _AuthFailure from e
            return "unknown"
        except (NoCredentialsError, PartialCredentialsError) as e:
            raise _AuthFailure from e
        except (HTTPClientError, BotoConnectionError):
            return "unknown"
        remote = {str(k).lower(): v for k, v in (head.get("Metadata") or {}).items()}
        expected = _object_metadata(meta)
        # Objects written before expires_at metadata existed lack it; when present it must match too.
        same = (head.get("ContentLength") == meta["bytes"]
                and all(remote.get(k) == expected[k] for k in _MATCH_FIELDS)
                and remote.get("expires_at", expected["expires_at"]) == expected["expires_at"])
        return "match" if same else "mismatch"

    def _finish(self, meta: dict[str, Any], result: DrainResult) -> None:
        if self._expired(meta):  # slow I/O carried us past expiry: remove nothing, recovery prunes it
            result.expired += 1
            return
        if self.spool.consume(meta, meta["sha256"]):
            result.uploaded += 1
        elif self._expired(meta):  # expired between the check above and the locked consume
            result.expired += 1
        else:
            result.kept += 1
            logger.warning("outbound audio drain: %s changed after upload; local entry kept",
                           self.spool.entry_key(meta))

    # ── drain lock (separate from the spool lock; held across network I/O) ──
    @contextmanager
    def _drain_locked(self) -> Iterator[None]:
        root = self.spool.settings.root
        st = os.lstat(root)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise OSError(errno.ENOTDIR, "spool root must be a real directory owned by this user", str(root))
        fd = os.open(root / DRAIN_LOCK_NAME,
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        try:
            lst = os.fstat(fd)
            if not stat.S_ISREG(lst.st_mode) or lst.st_uid != os.geteuid():
                raise OSError(errno.EPERM, "drain lock must be a regular file owned by this user", str(root))
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def build_drain(extra: Optional[dict[str, Any]], hermes_home: Path, spool: Optional[OutboundAudioSpool] = None,
                clock=time.time) -> Optional[PrivateAudioDrain]:
    """``None`` unless the drain is enabled and safe: no client, no spool object, no filesystem access."""
    try:
        settings = DrainSettings.from_extra(extra, hermes_home)
    except (TypeError, ValueError):
        logger.warning("outbound_audio_drain config invalid; drain stays disabled")
        return None
    if not settings.enabled:
        return None
    if botocore is None or fcntl is None:
        logger.warning("outbound_audio_drain needs botocore and POSIX flock; drain stays disabled")
        return None
    if spool is None:
        try:
            spool = OutboundAudioSpool(SpoolSettings.from_extra(extra, hermes_home), clock=clock)
        except (TypeError, ValueError, KeyError):
            logger.warning("outbound_audio_spool config invalid; drain stays disabled")
            return None
    drain = PrivateAudioDrain(spool, settings, clock=clock)
    return drain if drain._client is not None else None


def _telegram_extra(config_path: Optional[Path]) -> dict[str, Any]:
    if config_path is not None:
        import yaml

        loaded = yaml.safe_load(config_path.read_text())
        return loaded if isinstance(loaded, dict) else {}
    from gateway.config import Platform, load_gateway_config

    pconfig = load_gateway_config().platforms.get(Platform.TELEGRAM)
    return dict(pconfig.extra) if pconfig else {}


def main(argv: Optional[list[str]] = None) -> int:
    """Run exactly one foreground drain pass.

    Exit 0 ok/disabled, 1 when entries failed or collided, 2 when enabled but unusable (bad config,
    missing botocore/flock, or missing dedicated credentials) so a supervisor surfaces the misconfig.
    """
    parser = argparse.ArgumentParser(
        prog="python -m plugins.platforms.telegram.outbound_audio_drain",
        description="Drain the outbound audio spool to the private bucket once (foreground; never daemonizes).")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass (the only mode; accepted for explicit worker invocations)")
    parser.add_argument("--config", type=Path,
                        help="YAML/JSON mapping used as the Telegram platform `extra` block "
                             "(default: the gateway's Telegram config for the active HERMES_HOME)")
    args = parser.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from hermes_constants import get_hermes_home

    extra = _telegram_extra(args.config)
    drain = build_drain(extra, get_hermes_home())
    if drain is None:
        raw = extra.get("outbound_audio_drain") if isinstance(extra, dict) else None
        if isinstance(raw, dict) and raw.get("enabled") is True:
            logger.error("outbound audio drain enabled but unusable; see warnings above")
            return 2
        logger.info("outbound audio drain disabled; nothing to do")
        return 0
    result = drain.drain_once()
    logger.info("outbound audio drain pass: %s", json.dumps(asdict(result), sort_keys=True))
    return 1 if result.failed or result.collided else 0


if __name__ == "__main__":
    sys.exit(main())
