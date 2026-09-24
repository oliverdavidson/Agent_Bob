"""Web entry point. Serves health checks and, when BOB_RUN_WORKER is not "false", runs the
mailbox worker in a background thread of the same container."""

import logging
import os
import threading
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI
from sqlalchemy import func, select, text

from bob.agent.llm import make_client
from bob.agent.triage import triage_email
from bob.config import get_settings
from bob.db import make_engine, make_sessionmaker
from bob.models import Document, InboundEmail, Job
from bob.storage import make_storage
from bob.worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = get_settings()
engine = make_engine(settings.database_url)
SessionFactory = make_sessionmaker(engine)


def build_worker() -> Worker:
    from bob.mail.graph import GraphMailSource

    storage = make_storage(settings)
    client = make_client(settings)
    handlers = {
        "triage_email": lambda session, p: triage_email(
            session, p["email_id"], client, storage, settings
        ),
    }
    return Worker(SessionFactory, settings, GraphMailSource(settings), storage, handlers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = None
    if os.environ.get("BOB_RUN_WORKER", "true").lower() != "false":
        worker = build_worker()
        threading.Thread(target=worker.run_forever, name="bob-worker", daemon=True).start()
    yield
    if worker:
        worker.stop()


app = FastAPI(title="Bob", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    with SessionFactory() as session:
        session.execute(text("select 1"))
    return {"status": "ok"}


@app.get("/status")
def status() -> dict:
    """Counts for a quick look at the pipeline. Internal only (ingress restricted in Bicep)."""
    with SessionFactory() as session:
        count = partial(_group_count, session)
        return {
            "emails": count(InboundEmail.status),
            "documents": count(Document.status),
            "jobs": count(Job.status),
        }


def _group_count(session, column) -> dict[str, int]:
    return dict(session.execute(select(column, func.count()).group_by(column)).all())
