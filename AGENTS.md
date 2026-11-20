# Crucible API

FastAPI and SQLModel service on PostgreSQL that stores scientific records for the Crucible Data Platform.

## Rules

- Changes reach `main` only through a pull request that the user merges. Never push or force-push `main`.
- Never run a migration or write data on staging or production unless the user authorizes that operation.
- Every change to a table in `app/tables.py` needs an Alembic revision; keep a single head.
- Routes never take or return a table class; each gets its own model in `app/models/`.
- A change that API users, operators or clients can see gets one line under `Unreleased` in `CHANGELOG.md`.

## Where things are

| Path | Holds |
|---|---|
| `app/api/routes/` | Routes |
| `app/services/`, `app/utils/` | Logic shared by routes |
| `app/tables.py` | Database tables |
| `app/models/` | Request and response models |
| `app/auth/` | Sign-in: providers, passwords, clients, tokens, sessions |
| `app/storage/` | Storage adapters (`gcs`, `s3`) behind one interface |
| `app/cli/` | Server commands: `python -m app.cli --help` |
| `app/config.py` | Settings, read through `get_settings()` |
| `migrations/` | Alembic revisions and the schema baseline |
| `docs/` | Reference pages and guides: start at `docs/README.md` |

## Skills

Workflows are in `.agents/skills/`, one folder per skill with its `SKILL.md`.

| Skill | Use when |
|---|---|
| `run-checks` | Running tests, the PostgreSQL tests or the self-hosting job locally |
| `change-database-schema` | Editing a table, writing a migration or a data change |
| `add-or-change-route` | Adding, changing, deprecating or removing a route |
| `release` | Preparing a release pull request |
