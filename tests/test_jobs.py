from datetime import timedelta

import pytest

from bob import jobs
from bob.db import utcnow


def test_dedupe_key_prevents_second_job(factory):
    with factory() as s:
        assert jobs.enqueue(s, "triage_email", {"email_id": 1}, dedupe_key="triage:1")
        assert jobs.enqueue(s, "triage_email", {"email_id": 1}, dedupe_key="triage:1") is None
        s.commit()
        assert s.query(jobs.Job).count() == 1


def test_claim_complete(factory):
    with factory() as s:
        jobs.enqueue(s, "a", {"n": 1})
        s.commit()
        job = jobs.claim(s)
        assert job.status == "running" and job.attempts == 1
        assert jobs.claim(s) is None
        jobs.complete(s, job)
        s.commit()
        assert job.status == "done"


def test_delayed_job_is_not_claimed_early(factory):
    with factory() as s:
        jobs.enqueue(s, "a", {}, delay=timedelta(hours=1))
        s.commit()
        assert jobs.claim(s) is None


def test_fail_retries_then_gives_up(factory):
    with factory() as s:
        job = jobs.enqueue(s, "a", {})
        job.max_attempts = 2
        s.commit()

        job = jobs.claim(s)
        jobs.fail(s, job, "boom")
        assert job.status == "queued" and job.run_after > utcnow()

        job.run_after = utcnow() - timedelta(seconds=1)
        s.commit()
        job = jobs.claim(s)
        jobs.fail(s, job, "boom again")
        assert job.status == "failed"
        assert job.last_error == "boom again"


def test_two_workers_never_claim_the_same_job(factory):
    with factory() as s:
        if s.get_bind().dialect.name != "postgresql":
            pytest.skip("row locking needs Postgres (set BOB_TEST_DATABASE_URL)")
        jobs.enqueue(s, "a", {"n": 1})
        jobs.enqueue(s, "a", {"n": 2})
        s.commit()

    with factory() as first, factory() as second:
        a = jobs.claim(first)  # holds the row lock until commit
        b = jobs.claim(second)
        assert {a.payload["n"], b.payload["n"]} == {1, 2}
