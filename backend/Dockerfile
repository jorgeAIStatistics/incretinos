FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml constraints.txt alembic.ini ./
COPY incretinos ./incretinos
COPY alembic ./alembic
COPY fixtures ./fixtures
# The claim-verification benchmark sets read by `incretinos benchmark-claims` (dev and held-out; not loaded by the API).
COPY benchmarks ./benchmarks
# Reproducible install: every dependency is pinned by constraints.txt (generated from the environment the tests
# ran in), so two builds of the same commit contain the same versions. The second command lists anything pip had
# to resolve without a pin (a Linux-only dependency, or an optional extra); add such a package to constraints.txt.
# Optional extras are chosen at build time, e.g. --build-arg INCRETINOS_EXTRAS=llm for the Anthropic SDK.
ARG INCRETINOS_EXTRAS=""
RUN pip install -c constraints.txt ".${INCRETINOS_EXTRAS:+[$INCRETINOS_EXTRAS]}" \
 && pip freeze --exclude incretinos | sed 's/==.*//' | tr 'A-Z_' 'a-z-' | sort > /tmp/installed \
 && grep -v '^#' constraints.txt | sed 's/==.*//' | tr 'A-Z_' 'a-z-' | sort > /tmp/pinned \
 && echo "Installed without a pin in constraints.txt: $(comm -23 /tmp/installed /tmp/pinned | tr '\n' ' ')" \
 && rm -f /tmp/installed /tmp/pinned
# Run as an unprivileged user; the only writable path is the data directory.
RUN useradd --create-home --uid 10001 incretinos && mkdir -p /app/var && chown -R incretinos /app/var
USER incretinos
# Explicit locations: the package is installed into site-packages, so nothing may be guessed from its own path.
# INCRETINOS_ENV_FILE=none: the container is configured by environment variables only.
ENV INCRETINOS_FIXTURE_DIR=/app/fixtures INCRETINOS_CURATED_DIR=/app/incretinos/curation/data INCRETINOS_CACHE_DIR=/app/var/cache \
    INCRETINOS_MIGRATIONS_DIR=/app/alembic INCRETINOS_BENCHMARK_DIR=/app/benchmarks/claims INCRETINOS_ENV_FILE=none
EXPOSE 8000
# Migrate (the same code path as `incretinos init-db` anywhere else), seed from offline fixtures when the database
# is empty, then serve. exec makes the server PID 1 so that it receives the stop signal. --no-proxy-headers: the
# client address is the socket peer; X-Forwarded-For is read by the application, and only from the proxies listed
# in INCRETINOS_RATE_LIMIT_TRUSTED_PROXIES.
CMD ["sh", "-c", "incretinos init-db && python -m incretinos.bootstrap && exec uvicorn incretinos.api.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers"]
