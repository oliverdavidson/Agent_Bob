"""Background loop: poll the mailbox, then drain the job queue.

Runs as a thread inside the single container (see bob.main). Keep the Container App at one
replica until the mailbox poll is made safe for concurrent pollers.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from bob import jobs
from bob.config import Settings
from bob.mail.ingest import poll_mailbox
from bob.mail.source import MailSource
from bob.storage import Storage

log = logging.getLogger(__name__)

Handler = Callable[[Session, dict[str, Any]], None]


class Worker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        mail: MailSource,
        storage: Storage,
        handlers: dict[str, Handler],
        periodic: list[tuple[int, str]] | None = None,
    ):
        self.factory = factory
        self.settings = settings
        self.mail = mail
        self.storage = storage
        self.handlers = handlers
        # (interval_seconds, job kind): queued once per interval, deduplicated across restarts.
        self.periodic = periodic or []
        self._stop = threading.Event()
        self._last_poll = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        log.info("Worker started for %s", self.settings.mailbox)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Worker tick failed")
            self._stop.wait(2)

    def tick(self) -> None:
        if time.monotonic() - self._last_poll >= self.settings.mail_poll_seconds:
            self._last_poll = time.monotonic()
            with self.factory() as session:
                jobs.requeue_stale(session)
                session.commit()
            self._queue_periodic()
            ingested = poll_mailbox(self.factory, self.mail, self.storage, self.settings)
            if ingested:
                log.info("Ingested %d email(s)", ingested)
        while not self._stop.is_set() and self.run_one():
            pass

    def _queue_periodic(self) -> None:
        with self.factory() as session:
            for interval, kind in self.periodic:
                bucket = int(time.time() // interval)
                jobs.enqueue(session, kind, {}, dedupe_key=f"{kind}:{interval}:{bucket}")
            session.commit()

    def run_one(self) -> bool:
        """Run a single job. Returns False when the queue is empty."""
        with self.factory() as session:
            job = jobs.claim(session, list(self.handlers))
            if job is None:
                return False
            job_id, kind, payload = job.id, job.kind, dict(job.payload)
            session.commit()  # release the row lock; status=running marks it as taken

        with self.factory() as session:
            job = session.get(jobs.Job, job_id)
            try:
                self.handlers[kind](session, payload)
                jobs.complete(session, job)
                session.commit()
            except Exception as exc:
                session.rollback()
                log.exception("Job %s (%s) failed", job_id, kind)
                job = session.get(jobs.Job, job_id)
                jobs.fail(session, job, f"{type(exc).__name__}: {exc}")
                session.commit()
        return True
