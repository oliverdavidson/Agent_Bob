FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY bob ./bob
COPY migrations ./migrations
RUN pip install --no-cache-dir . && useradd --create-home bob

USER bob
EXPOSE 8000
# One replica: migrate, then serve the API with the mailbox worker in a background thread.
CMD ["sh", "-c", "alembic upgrade head && uvicorn bob.main:app --host 0.0.0.0 --port 8000"]
