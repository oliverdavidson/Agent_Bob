from sqlalchemy import select

from bob.models import Job
from bob.worker import Worker
from tests.conftest import FakeMail, make_message


def test_worker_ingests_then_runs_handler(factory, storage, settings):
    handled = []
    mail = FakeMail(messages=[make_message(1)])
    worker = Worker(
        factory,
        settings,
        mail,
        storage,
        {"triage_email": lambda s, p: handled.append(p["email_id"])},
    )

    worker.tick()

    assert handled == [1]
    with factory() as s:
        assert s.scalars(select(Job.status)).one() == "done"


def test_failing_handler_is_retried_later(factory, storage, settings):
    def boom(session, payload):
        raise ValueError("model timeout")

    worker = Worker(
        factory, settings, FakeMail(messages=[make_message(1)]), storage, {"triage_email": boom}
    )
    worker.tick()

    with factory() as s:
        job = s.scalars(select(Job)).one()
        assert (job.status, job.attempts) == ("queued", 1)
        assert "model timeout" in job.last_error
