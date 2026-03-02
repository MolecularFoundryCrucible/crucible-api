FROM python:3.11-trixie
#UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

USER root
WORKDIR /root/

# Disable development dependencies
ENV UV_NO_DEV=1

ARG githash
ENV GITHASH=$githash

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
CMD exec /bin/uv run uvicorn app.api.main:app --host 0.0.0.0 --reload --port 7777
