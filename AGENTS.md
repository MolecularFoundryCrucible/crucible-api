# Crucible API

FastAPI and SQLModel service for the Molecular Foundry's scientific records. The Foundry runs it on GKE, deployed from the mf-crucible-deploy repository; labs self-host it with `deploy/compose/`.

## Commands

- Install: `uv sync --locked`
- Tests: `uv run pytest -q`. PostgreSQL tests skip unless `CRUCIBLE_TEST_PG_URL` is set, and S3 tests unless `S3_TEST_ENDPOINTS` is set (see `.github/workflows/tests.yml`).
- One migration head: `uv run alembic heads` prints a single revision.
- Server commands: `python -m app.cli` (`db upgrade`, `settings check`, `storage check`, `storage cleanup`, `files restore`, `admin create`).

## Layout

- `app/api/routes/` routes, `app/services/` logic, `app/models/` tables and schemas, `app/cli/` server commands.
- `app/config.py` settings, read once through `get_settings()`; `tests/conftest.py` clears the caches between tests.
- `app/storage/` storage adapters behind `ObjectStore`: `GoogleStore` (`STORAGE_BACKEND=gcs`) and `S3Store` (`s3`). Clients see the same routes on both; provider differences stay in the adapters.
- `migrations/versions/` Alembic revisions; `migrations/data/` one-off audits and backfills.
- `docs/` design notes; `deploy/compose/README.md` the self-hosting guide.

## Conventions

- A file's `storage_backend = "gcs"` means "stored by the API", on any store.
- No code comments unless the reason is not obvious from the code.

## Git workflow

- `main` is the only long-lived branch. Changes reach `main` through a reviewed pull request; the user merges.
- Never push directly to `main`, even when branch protection permits it.
- Never force-push `main`.
- A merge to `main` builds the image staging runs; production deploys only from release tags through mf-crucible-deploy.

## Change history

- Add an entry under `Unreleased` in `CHANGELOG.md` for changes that affect API users, operators, or downstream clients: one line per change, naming the settings, routes, or migration involved.
- Internal-only changes do not require a changelog entry.

## Database changes

- Every change to a `table=True` SQLModel requires a matching Alembic revision in `migrations/versions/`.
- Keep a single linear Alembic head and verify the revision chain after schema changes.
- Put one-time audits and backfills in `migrations/data/`. They must default to read-only or rollback behavior and require an explicit commit flag for writes.
- Do not run a migration or data write against staging or production unless the user explicitly authorizes that operation.
