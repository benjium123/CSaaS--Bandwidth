FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/requirements.lock* /app/
COPY backend/app /app/app
COPY backend/migrations /app/migrations
COPY backend/alembic.ini /app/alembic.ini

# Prefer the lock file (8.23) for a reproducible build; fall back to pyproject.toml's
# loose ranges if no lock has been generated yet.
RUN if [ -f /app/requirements.lock ]; then \
        pip install --no-cache-dir -r /app/requirements.lock && pip install --no-cache-dir --no-deps . ; \
    else \
        pip install --no-cache-dir . ; \
    fi \
    && apt-get purge -y build-essential && apt-get autoremove -y

# D2: csaas_media (docker-compose.prod.yml) is a named volume mounted at
# /app/var/media. Docker seeds a brand-new named volume from whatever already exists at
# that path in the image, owned by whatever owns it there. Without this mkdir the path
# doesn't exist in the image, so Docker creates the volume as an empty root-owned dir -
# the csaas user (below) then can't write media/recordings/transcripts into it. Must run
# BEFORE the chown -R so it's swept up too.
RUN mkdir -p /app/var/media
RUN useradd --create-home --uid 10001 csaas && chown -R csaas:csaas /app
USER csaas

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# --workers 1 is LOAD-BEARING: the sweeper (app/sweeper) runs in-process and assumes a
# single instance (P14 8.18 adds pg_try_advisory_lock, but do not use this as license to
# raise worker count without also verifying sweeper concurrency behavior end-to-end).
# --proxy-headers/--forwarded-allow-ips: nginx sits in front in prod, so trust its
# X-Forwarded-* headers for scheme/client-IP (uvicorn only sees the loopback hop otherwise).
CMD ["uvicorn", "app.main:app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
