FROM python:3.11-trixie
#UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

USER root
WORKDIR /root/

# Disable development dependencies
ENV UV_NO_DEV=1

ARG githash
ENV GITHASH=$githash
ARG api_version
ENV API_VERSION=$api_version
ARG BRANCH_NAME
ENV BRANCH_NAME=$BRANCH_NAME
RUN if [ "$BRANCH_NAME" = "main" ]; then echo 7777 > /root/.port; else echo 8889 > /root/.port; fi

# rclone
RUN curl https://rclone.org/install.sh | bash

# files
COPY app/ /root/app/
COPY pyproject.toml /root/
COPY uv.lock /root/
COPY .python-version /root/
RUN mkdir uploads
RUN mkdir download-zips

RUN /bin/uv sync --locked 

# Run app
#CMD python -m app.api.test
#CMD exec /bin/uv run uvicorn app.api.main:app --host 0.0.0.0 --reload --port $(cat /root/.port)
CMD exec /bin/uv run gunicorn app.api.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 4 \
  --bind 0.0.0.0:$(cat /root/.port)

