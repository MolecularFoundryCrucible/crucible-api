FROM python:3.11-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

WORKDIR /app

# Disable development dependencies, and compile bytecode now: at run time the code is read-only.
ENV UV_NO_DEV=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1
ENV PATH="/app/.venv/bin:$PATH"

# dependencies first, so code-only changes reuse this layer
COPY pyproject.toml uv.lock .python-version /app/
RUN /bin/uv sync --locked --no-install-project

ARG githash
ENV GITHASH=$githash

ARG api_version
ENV API_VERSION=$api_version

# files
COPY app/ /app/app/
COPY alembic.ini /app/alembic.ini
COPY migrations/ /app/migrations/
COPY scripts/cleanup_hub_auth.py /app/scripts/cleanup_hub_auth.py

RUN /bin/uv sync --locked && python -m compileall -q /app/app /app/migrations /app/scripts

# The code stays owned by root, so the API cannot change it. The old /root paths remain as links
# until every deployment calls /app directly.
RUN groupadd --system --gid 10001 crucible \
  && useradd --system --uid 10001 --gid crucible --no-create-home --home-dir /tmp --shell /usr/sbin/nologin crucible \
  && ln -s /app/.venv /root/.venv && ln -s /app/scripts /root/scripts && chmod 755 /root
ENV HOME=/tmp
USER 10001:10001

# Run app. A worker still loading the code and reading its secrets counts as silent, and on a busy
# node that has taken 25 of the default 30 seconds; the timeout does not limit request length here.
# Worker heartbeats go to /dev/shm, a memory filesystem, as gunicorn advises for containers.
CMD exec gunicorn app.api.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers "${WEB_CONCURRENCY:-4}" \
  --timeout 60 \
  --worker-tmp-dir /dev/shm \
  --bind "0.0.0.0:${PORT:-8000}"
