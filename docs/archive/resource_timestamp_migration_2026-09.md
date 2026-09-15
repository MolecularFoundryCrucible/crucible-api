# Resource timestamp migration record

Status: completed in production on 2026-09-15.

Production deployed API commit `24a24ab9ce74b01f841750df7003091ce4b57119` and Alembic revision `3f2b6a9c1d84`. The direct schema migration completed in 27.6 seconds while the service remained available. Cloud SQL backup `1789478163170` completed before the write. Health checks, authenticated collection smoke tests, and rollback-only trigger verification passed after deployment.

The migration converted API-owned `idtype.creation_time` and `idtype.modification_time` from text to PostgreSQL `TIMESTAMPTZ` and installed database enforcement for generated timestamps, immutable creation time, and automatically advanced modification time. The producer-supplied `dataset.timestamp` field was not changed.

## Applied legacy conversion policy

- Preserve null creation times as null.
- Use creation time for a missing modification time when creation is known; otherwise preserve null.
- Preserve explicitly offset timestamps as the same instant.
- Interpret timezone-less timestamps in `America/Los_Angeles` using the date's applicable offset.
- Interpret dashed and compact date-only values as midnight in `America/Los_Angeles`.
- Interpret `YYYY-MM-DD_HHMMSS` as local `America/Los_Angeles` time.
- Interpret compact `_am` values as midnight and `_pm` values as noon in `America/Los_Angeles`.
- Convert empty strings and `NaN` sentinels to null.
- Reject every other unparseable non-null value.

## Audit evidence

The final read-only production audit scanned 411,803 resources. It found no unsupported formats.

Creation time contained 45,914 nulls, 859 date-only values, 104,349 timezone-less values with a time, 203,535 explicitly non-UTC values, 57,140 UTC values, and 6 missing sentinels.

Modification time contained 45,830 nulls, 287 date-only values, 89,323 timezone-less values with a time, 197,115 explicitly non-UTC values, 79,197 UTC values, 45 compact `am` or `pm` values, and 6 missing sentinels.

This file is historical evidence, not a reusable production runbook. Do not replay the migration against a current environment.
