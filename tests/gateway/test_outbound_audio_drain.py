"""HOLOC-26 private drain contract with a synthetic, in-memory S3 transport."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from plugins.platforms.telegram import outbound_audio_drain as drain
from plugins.platforms.telegram.outbound_audio_spool import OutboundAudioSpool, SpoolSettings
from tests.gateway.test_outbound_audio_spool import record

pytestmark = pytest.mark.platforms("posix")


class MemoryS3:
    def __init__(self, *, put_error=None, head_change=None):
        self.put_error, self.head_change = put_error, head_change
        self.calls, self.objects = [], {}

    def put_object(self, **kwargs):
        self.calls.append(("PUT", kwargs))
        assert kwargs["IfNoneMatch"] == "*"
        self.objects[kwargs["Key"]] = kwargs
        if self.put_error:
            raise self.put_error
        return {"ETag": "not-proof"}

    def head_object(self, **kwargs):
        self.calls.append(("HEAD", kwargs))
        obj = self.objects[kwargs["Key"]]
        head = {"ContentLength": len(obj["Body"]), "Metadata": dict(obj["Metadata"])}
        if self.head_change:
            self.head_change(head)
        return head


def seeded(tmp_path):
    spool = OutboundAudioSpool(SpoolSettings(True, tmp_path / "queue", frozenset({101})))
    assert spool.store(record(), b"synthetic-only")
    return spool


@pytest.mark.parametrize("spool_on,drain_on", [(False, False), (True, False), (False, True)])
def test_either_disabled_gate_precedes_credentials_and_queue(tmp_path, monkeypatch, spool_on, drain_on):
    monkeypatch.setattr(drain, "_new_client", lambda *a: pytest.fail("credential access"))
    extra = {"outbound_audio_spool": {"enabled": spool_on},
             "outbound_audio_drain": {"enabled": drain_on}}
    assert drain.build_drain(extra, tmp_path) is None
    spool = OutboundAudioSpool(SpoolSettings(spool_on, tmp_path / "absent", frozenset()))
    worker = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=drain_on))
    assert worker.drain_once() == drain.DrainResult()
    assert not spool.settings.root.exists()


@pytest.mark.parametrize("settings", [
    drain.DrainSettings(enabled=False),
    drain.DrainSettings(enabled=True, endpoint_url="http://s3.delo.sh"),
    drain.DrainSettings(enabled=True, endpoint_url="https://s3.delo.sh.evil.invalid"),
    drain.DrainSettings(enabled=True, endpoint_url="https://s3.delo.sh/"),
    drain.DrainSettings(enabled=True, bucket="earshot"),
    drain.DrainSettings(enabled=True, bucket="other"),
    drain.DrainSettings(enabled=True, prefix="../"),
])
def test_unsafe_settings_never_construct_a_client(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(drain, "_new_client", lambda *a: pytest.fail("unsafe credential access"))
    spool = seeded(tmp_path)
    assert drain.PrivateAudioDrain(spool, settings).drain_once() == drain.DrainResult()
    assert len(spool.pending()) == 1


@pytest.mark.parametrize("error", [None, EndpointConnectionError(endpoint_url=drain.ALLOWED_ENDPOINT),
    ClientError({"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject"),
    ClientError({"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "PutObject")])
def test_matching_head_is_required_for_success_or_ambiguous_put(tmp_path, error):
    spool, client = seeded(tmp_path), MemoryS3(put_error=error)
    worker = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=True), client)
    assert worker.drain_once().uploaded == 1
    assert [c[0] for c in client.calls] == ["PUT", "HEAD"]
    assert spool.pending() == []


@pytest.mark.parametrize("field", ["sha256", "bot_id", "chat_id", "message_id", "sent_at", "expires_at"])
def test_success_etag_without_matching_identity_head_never_consumes(tmp_path, field):
    spool = seeded(tmp_path)
    client = MemoryS3(head_change=lambda head: head["Metadata"].pop(field))
    worker = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=True), client)
    assert worker.drain_once().uploaded == 0
    assert len(spool.pending()) == 1
    first_key = client.calls[0][1]["Key"]
    assert worker.drain_once().uploaded == 0
    assert client.calls[2][1]["Key"] == first_key


def test_auth_failure_stops_the_pass_without_consuming(tmp_path):
    spool = seeded(tmp_path)
    assert spool.store(record(2), b"second")
    error = ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
    client = MemoryS3(put_error=error)
    result = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=True), client).drain_once()
    assert result.failed == 1 and result.kept == 1
    assert len(spool.pending()) == 2 and len(client.calls) == 1


def test_expiry_during_upload_is_not_acknowledged(tmp_path):
    spool = seeded(tmp_path)
    now = time.time()
    client = MemoryS3(head_change=lambda head: clock.append(now + 1800))
    clock = [now]
    worker = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=True), client, clock=lambda: clock[-1])
    result = worker.drain_once()
    assert result.expired == 1 and result.uploaded == 0
    assert len(spool.pending()) == 1


def test_missing_dedicated_credentials_never_use_default_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(drain.botocore.session, "Session", lambda: pytest.fail("default credential chain"))
    for environ in ({}, {drain.ACCESS_KEY_ENV: "op://DeLoSecrets/Test/access", drain.SECRET_KEY_ENV: "placeholder"}):
        assert drain._new_client(drain.DrainSettings(enabled=True), environ) is None


def test_overlapping_passes_serialize_and_do_not_duplicate_upload(tmp_path):
    spool, client = seeded(tmp_path), MemoryS3()
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    put = client.put_object

    def blocked_put(**kwargs):
        entered.set()
        assert release.wait(10)
        return put(**kwargs)

    client.put_object = blocked_put
    worker = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=True), client)

    def second():
        second_started.set()
        return worker.drain_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker.drain_once)
        assert entered.wait(10)
        other = pool.submit(second)
        assert second_started.wait(10)
        release.set()
        results = [first.result(timeout=10), other.result(timeout=10)]
    assert sum(r.uploaded for r in results) == 1
    assert [method for method, _ in client.calls] == ["PUT", "HEAD"]


def test_conditional_collision_is_kept_not_overwritten(tmp_path):
    spool = seeded(tmp_path)
    error = ClientError(
        {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
    client = MemoryS3(put_error=error, head_change=lambda head: head.update(ContentLength=999))
    result = drain.PrivateAudioDrain(spool, drain.DrainSettings(enabled=True), client).drain_once()
    assert result.collided == 1 and result.uploaded == 0
    assert len(spool.pending()) == 1


def test_client_uses_only_explicit_dedicated_credentials_and_bounded_signing(monkeypatch):
    seen = {}

    class Session:
        def set_config_variable(self, name, value):
            seen[name] = value

        def create_client(self, service, **kwargs):
            seen.update(service=service, **kwargs)
            return "synthetic-client"

    monkeypatch.setattr(drain.botocore.session, "Session", Session)
    client = drain._new_client(drain.DrainSettings(enabled=True), {
        drain.ACCESS_KEY_ENV: "unit-id", drain.SECRET_KEY_ENV: "unit-secret",
        "AWS_ACCESS_KEY_ID": "not-the-dedicated-pair", "AWS_SECRET_ACCESS_KEY": "not-used",
    })
    assert client == "synthetic-client"
    assert seen["config_file"] == seen["credentials_file"] == os.devnull
    assert seen["aws_access_key_id"] == "unit-id" and seen["aws_secret_access_key"] == "unit-secret"
    assert seen["endpoint_url"] == drain.ALLOWED_ENDPOINT
    assert seen["config"].signature_version == "s3v4"
    assert seen["config"].retries == {"max_attempts": 0}
    assert seen["config"].s3["addressing_style"] == "path"
