"""HOLOC-26: owner-side job-copy provenance reaches actual scheduler media delivery."""
import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from tests.gateway.outbound_audio_fixtures import make_adapter

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_result,target,enabled,expected", [
    (True, "101", True, 1),
    (True, "202", True, 0),
    (False, "101", True, 0),
    (True, "101", False, 0),
])
async def test_owner_job_copy_carries_proof_back_to_firer_before_native_send(
        tmp_path, monkeypatch, audio_result, target, enabled, expected):
    from cron import scheduler, scheduler_delivery
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    adapter = make_adapter(tmp_path, monkeypatch, enabled=enabled)
    audio = tmp_path / "synthetic.ogg"
    audio.write_bytes(b"cron-egress")
    response = f"MEDIA:{audio}" if audio_result else "Text-only job result."
    config = {"telegram": {"extra": {"outbound_audio_spool": {"enabled": enabled}}}}
    monkeypatch.setattr(run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr(run, "_profile_runtime_scope", lambda home: nullcontext())
    seen = []

    def agent_execution(job, **kwargs):
        # Only inference is stubbed: the real owner job-copy, flags and scheduler egress run below.
        seen.append(job)
        return True, "synthetic doc", response, None

    monkeypatch.setattr(scheduler, "run_job", agent_execution)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="cron")
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id="p", instance_id="test", db=db,
                                 epoch=begin_runtime_epoch(db, instance_id="test"))
    authority.sessions["s"] = LiveSession(SimpleNamespace(platform=None, user_id="cron-owner"), "route")
    job = {"id": "job", "origin": {"platform": "telegram", "chat_id": "101"}}
    request_id = "cron:job:f1"
    try:
        with db:
            admit_session_input(db, epoch=authority.epoch, principal_id="cron-owner", session_id="s",
                                request_id=request_id, payload={"text": ""})
            row = claim_session_input(db, epoch=authority.epoch, session_id="s")
            policy = SimpleNamespace(request_json=json.dumps({
                "extra_prompt": None, "request_id": request_id, "cron_job": job}))
            assert await session_cron.execute(authority, SessionRef("p", "s"), row, policy) == response
        flags = authority.pending_results[row["admission_id"]]["result"]["cron_job_flags"]
        assert seen[0] is not job and "_outbound_audio_reply" not in job
        assert bool(flags.get("_outbound_audio_reply")) == (audio_result and enabled)
        # Exactly the scheduler_authority hand-back: the FIRER has a different dictionary.
        firer_job = dict(job, execution_id="f1")
        firer_job.update(flags)
        assert await asyncio.to_thread(
            scheduler_delivery._send_media_via_adapter, adapter, target, [(str(audio), True)],
            None, asyncio.get_running_loop(), firer_job) == []
        assert len(adapter._bot.sent) == 1
        if enabled:
            assert len(adapter._outbound_audio_spool.pending()) == expected
        else:
            assert adapter._outbound_audio_spool is None
    finally:
        db.close()


def test_cron_cannot_reuse_another_fire_or_an_unrelated_attachment(tmp_path, monkeypatch):
    from plugins.platforms.telegram.outbound_audio_provenance import (
        CRON_REPLY_FLAG, cron_reply_metadata, stamp_cron_reply,
    )
    adapter = make_adapter(tmp_path, monkeypatch)
    job = {"id": "job", "execution_id": "f1", "origin": {"platform": "telegram", "chat_id": "101"}}
    stamp_cron_reply(job, "MEDIA:/synthetic.ogg", "f1",
                     {"telegram": {"extra": {"outbound_audio_spool": {"enabled": True}}}})
    assert CRON_REPLY_FLAG in job
    assert cron_reply_metadata(adapter, job, "101", "/synthetic.ogg", None)
    assert cron_reply_metadata(adapter, dict(job, execution_id="f2"), "101", "/synthetic.ogg", None) is None
    assert cron_reply_metadata(adapter, job, "101", "/unrelated.ogg", None) is None
    assert cron_reply_metadata(adapter, job, "-101", "/synthetic.ogg", None) is None
