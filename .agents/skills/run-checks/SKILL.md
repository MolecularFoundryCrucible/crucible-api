---
name: run-checks
description: Use when about to open a pull request, when asked whether something passes, or to reproduce a CI job locally - the test suite, the PostgreSQL-backed tests, pyflakes, actionlint and the self-hosting job.
---

# Run the checks

Report only checks you ran, with their output. Pick the smallest set that covers the change, then widen.

## 1. Tests

```bash
uv sync --locked
uv run pytest -q                      # everything that needs no services
uv run pytest -q tests/test_x.py      # one file while working
uv run python -m pyflakes app scripts # unused or undefined names
```

Tests never read a local `.env`. The suite takes about three minutes.

## 2. PostgreSQL-backed tests

About 120 tests skip without a server. They need PostgreSQL 17 with pgvector and four databases:

```bash
docker run -d --rm --name crucible-pg-test -e POSTGRES_PASSWORD=test -p 55432:5432 pgvector/pgvector:0.8.7-pg17
for db in crucible_pg_test crucible_hub_auth_test crucible_slug_index_test crucible_email_index_test; do
  docker exec crucible-pg-test psql -U postgres -c "CREATE DATABASE $db"
done
docker exec crucible-pg-test psql -U postgres -d crucible_pg_test -c "CREATE EXTENSION vector" -c "CREATE EXTENSION pg_trgm"

B=postgresql+psycopg2://postgres:test@127.0.0.1:55432
CRUCIBLE_TEST_PG_URL=$B/crucible_pg_test HUB_AUTH_TEST_DATABASE_URL=$B/crucible_hub_auth_test \
SLUG_INDEX_TEST_DATABASE_URL=$B/crucible_slug_index_test EMAIL_INDEX_TEST_DATABASE_URL=$B/crucible_email_index_test \
  uv run pytest -q
```

Run these for any change to `app/tables.py`, `migrations/`, sign-in, or SQL that SQLite treats differently.

## 3. The self-hosting job

For changes to the `Dockerfile`, startup, settings, or sign-in pages, run the CI job itself. It builds the image and runs it in the crucible-selfhost bundle (a sibling checkout, else cloned), in a temporary copy of the checkout, and removes it afterwards:

```bash
uv run python .agents/skills/run-checks/run_ci_job.py selfhost
```

It takes a few minutes and needs Docker with port 8080 free on 127.0.0.1. The bundle's own jobs, with the Hub and ORCID, run in crucible-selfhost. The last line is `JOB PASSED` or `JOB FAILED`.

## 4. Workflow files

After editing `.github/workflows/`:

```bash
docker run --rm -v "$PWD":/repo:ro -w /repo rhysd/actionlint:1.7.7
```

## Never

- Point a test at the staging or production database, or create users there.
- Print a secret or an environment file's contents.
