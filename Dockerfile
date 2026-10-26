FROM python:3.11-trixie
#UV
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

USER root
WORKDIR /root/

# Disable development dependencies
ENV UV_NO_DEV=1
ENV PATH="/root/.venv/bin:$PATH"

# dependencies first, so code-only changes reuse this layer
COPY pyproject.toml uv.lock .python-version /root/
RUN /bin/uv sync --locked --no-install-project

ARG githash
ENV GITHASH=$githash

ARG api_version
ENV API_VERSION=$api_version
RUN echo "API_VERSION: $API_VERSION"

ARG BRANCH_NAME
ENV BRANCH_NAME=$BRANCH_NAME

# files
COPY app/ /root/app/
COPY migrations/versions/ /root/migrations/versions/
COPY scripts/cleanup_hub_auth.py /root/scripts/cleanup_hub_auth.py
RUN mkdir uploads
RUN mkdir download-zips

RUN /bin/uv sync --locked

# Run app
CMD exec gunicorn app.api.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers "${WEB_CONCURRENCY:-4}" \
  --bind "0.0.0.0:${PORT:-8000}"
