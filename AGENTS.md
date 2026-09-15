# Repository Agent Instructions

## Git workflow

- Changes must reach `main` through a pull request from `staging` or another reviewed branch.
- Never push directly to `main`, even when branch protection permits it.
- Never force-push `main`.
- Pushing to feature branches or `staging` is allowed when the user authorizes the release workflow.

## Change history

- Add an entry under `Unreleased` in `CHANGELOG.md` for changes that affect API users, operators, or downstream clients.
- Internal-only changes do not require a changelog entry.

## Database changes

- Every change to a `table=True` SQLModel requires a matching Alembic revision in `migrations/versions/`.
- Keep a single linear Alembic head and verify the revision chain after schema changes.
- Put one-time audits and backfills in `migrations/data/`. They must default to read-only or rollback behavior and require an explicit commit flag for writes.
- Do not run a migration or data write against staging or production unless the user explicitly authorizes that operation.
