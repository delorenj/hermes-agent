"""``scheduler: external`` (Candybar AD-19): the ticker never fires the job on its own, a
``trigger_job`` makes it due exactly once, and the trigger does not re-enable the natural schedule.

Real store against a temp HERMES_HOME, no mocks.
"""
from datetime import timedelta

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _make_due(job_id):
    """Rewind next_run_at into the past: the instant the NATURAL schedule would have fired."""
    from cron.jobs import _hermes_now, update_job

    past = (_hermes_now() - timedelta(seconds=30)).isoformat()
    update_job(job_id, {"next_run_at": past})


def _due_ids():
    from cron.jobs import get_due_jobs

    return [j["id"] for j in get_due_jobs()]


def test_internal_job_still_fires_when_due(temp_home):
    from cron.jobs import create_job

    job = create_job(prompt="x", schedule="every 5m", name="internal")
    _make_due(job["id"])
    assert job["id"] in _due_ids()


def test_external_job_never_fires_on_its_natural_schedule(temp_home):
    from cron.jobs import create_job, get_job

    job = create_job(prompt="x", schedule="every 5m", name="ext", scheduler="external")
    assert get_job(job["id"])["scheduler"] == "external"
    _make_due(job["id"])
    assert job["id"] not in _due_ids()
    # Not even after an hour of "missed" fires (catch-up / fast-forward must not resurrect it).
    from cron.jobs import _hermes_now, update_job

    update_job(job["id"], {"next_run_at": (_hermes_now() - timedelta(hours=3)).isoformat()})
    assert job["id"] not in _due_ids()


def test_trigger_makes_external_job_due_once_and_keeps_it_external(temp_home):
    from cron.jobs import create_job, get_job, mark_job_run, trigger_job

    job = create_job(prompt="x", schedule="every 5m", name="ext", scheduler="external")
    trigger_job(job["id"])
    assert job["id"] in _due_ids()

    # The run finishes: mark_job_run advances to the next NATURAL instant.
    assert mark_job_run(job["id"], True) is True
    after = get_job(job["id"])
    assert after["scheduler"] == "external"
    assert after["enabled"] is True
    _make_due(job["id"])
    assert job["id"] not in _due_ids(), "a trigger must not re-enable the natural schedule"


def test_provider_claim_without_trigger_is_refused_for_external_jobs(temp_home):
    from cron.jobs import claim_job_for_fire, create_job, trigger_job

    job = create_job(prompt="x", schedule="every 5m", name="ext", scheduler="external")
    _make_due(job["id"])
    assert claim_job_for_fire(job["id"]) is False
    trigger_job(job["id"])
    assert claim_job_for_fire(job["id"]) is True
    assert claim_job_for_fire(job["id"]) is False  # one attempt only


def test_manual_claim_still_runs_an_external_job(temp_home):
    """``hermes cron run`` (inline) claims with manual=True: that is the Candybar trigger path."""
    from cron.jobs import claim_job_for_fire, create_job

    job = create_job(prompt="x", schedule="every 5m", name="ext", scheduler="external")
    assert claim_job_for_fire(job["id"], manual=True) is True
    assert claim_job_for_fire(job["id"], manual=True) is False


def test_scheduler_normalization_and_update(temp_home):
    from cron.jobs import create_job, get_job, update_job

    plain = create_job(prompt="x", schedule="every 5m", name="plain", scheduler="internal")
    assert "scheduler" not in plain
    with pytest.raises(ValueError, match="Invalid scheduler"):
        create_job(prompt="x", schedule="every 5m", name="bad", scheduler="candybar")

    update_job(plain["id"], {"scheduler": "external"})
    assert get_job(plain["id"])["scheduler"] == "external"
    update_job(plain["id"], {"scheduler": "internal"})
    _make_due(plain["id"])
    assert plain["id"] in _due_ids()
    with pytest.raises(ValueError, match="Invalid scheduler"):
        update_job(plain["id"], {"scheduler": "nope"})


def test_scheduler_is_an_authored_definition_field():
    from cron.job_definition import JOB_DEFINITION_FIELDS

    assert "scheduler" in JOB_DEFINITION_FIELDS
