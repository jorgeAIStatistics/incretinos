FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY incretinos ./incretinos
COPY alembic ./alembic
COPY fixtures ./fixtures
RUN pip install --upgrade pip && pip install .
# Run as an unprivileged user; the only writable path is the data directory.
RUN useradd --create-home --uid 10001 incretinos && mkdir -p /app/var && chown -R incretinos /app/var
USER incretinos
ENV INCRETINOS_FIXTURE_DIR=/app/fixtures INCRETINOS_CURATED_DIR=/app/incretinos/curation/data INCRETINOS_CACHE_DIR=/app/var/cache
EXPOSE 8000
# Migrate, seed from offline fixtures when the database is empty, then serve.
CMD ["sh", "-c", "alembic upgrade head && python -m incretinos.bootstrap && uvicorn incretinos.api.main:app --host 0.0.0.0 --port 8000"]
