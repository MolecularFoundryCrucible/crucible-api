# Resource timestamp migration runbook

## Scope

This runbook covers the direct conversion of API-owned `idtype.creation_time` and `idtype.modification_time` lifecycle fields from text to PostgreSQL `TIMESTAMPTZ` while the production API pods remain running.

The producer-supplied `dataset.timestamp` field is outside this migration. It remains a text field, is not required to contain UTC or a parseable timestamp, and must not be normalized, typed, backfilled, or used to repair either lifecycle timestamp.

## Availability expectations

- Run the migration during a low-traffic period.
- Keep the production deployment and HPA running. Do not scale, delete, or restart production pods without a separately approved plan.
- The two `ALTER COLUMN TYPE` operations require an exclusive table lock and rewrite `idtype`. Requests using that table may wait while the transaction runs.
- The old API model is not compatible with the converted columns. The new deployment must already be starting and must complete immediately after the migration commits.
- The new deployment declares `EXPECTED_DB_REVISIONS=3f2b6a9c1d84`. Its pods remain unready while production is still at `b7e4a1c96d38`.
- The previous pod may remain in service briefly after the migration commits. Monitor the rollout continuously and stop if the new pod does not become ready promptly.

## Lifecycle policy

- Do not accept `creation_time` or `modification_time` in public create or update request models.
- Generate both lifecycle timestamps from the database server's current time when a resource is created.
- Keep creation time immutable after insertion.
- Set modification time from the database server's current time whenever `idtype` is updated.
- Preserve null creation timestamps as null.
- Populate a missing modification timestamp from creation time when creation is known. Leave both values null when creation is unknown.
- Preserve explicitly offset timestamps as the same instant.
- Interpret timezone-less timestamps in `America/Los_Angeles` using the applicable standard or daylight-saving offset for their date.
- Interpret dashed and compact date-only values as `00:00:00` in `America/Los_Angeles`.
- Interpret `YYYY-MM-DD_HHMMSS` as the encoded local time in `America/Los_Angeles`.
- Interpret compact `_am` values as `00:00:00` and `_pm` values as `12:00:00` in `America/Los_Angeles`.
- Convert empty strings and `NaN` sentinels to null.
- Stop for every other unparseable non-null value.

## Production audit evidence

The read-only production audit on 2026-09-14 scanned 411,803 resources through the Cloud SQL Auth Proxy in 42.93 seconds after detailed legacy-format classification.

### Creation time

- 45,914 null values.
- 859 date-only values. This includes 362 `YYYY-MM-DD` values and 497 dataset values using `YYYYMMDD`.
- 104,349 timezone-less values containing a time.
- 203,535 explicitly non-UTC values.
- 57,140 UTC values.
- 6 missing sentinels: four samples contain `NaN`, and two datasets contain an empty string.
- No unsupported formats.

### Modification time

- 45,830 null values.
- 287 `YYYY-MM-DD` date-only values.
- 89,323 timezone-less values containing a time. Five use `YYYY-MM-DD_HHMMSS`.
- 197,115 explicitly non-UTC values.
- 79,197 UTC values.
- 45 stock-solution recipe datasets use a compact date with an `am` or `pm` marker. The values are `20251209_am` (4), `20260116_pm` (14), `20260206_pm` (6), `20260210_pm` (1), `20260302_pm` (5), `20260316_pm` (1), `20260317_pm` (6), and `20260323_pm` (8).
- 6 missing sentinels matching the same four `NaN` samples and two empty-string datasets found in creation time.
- No unsupported formats.

## Release gates

1. Confirm the production API is healthy at the expected old commit and reports revision `b7e4a1c96d38`.
2. Confirm staging is healthy at the reviewed release commit and reports revision `3f2b6a9c1d84`.
3. Run the complete test suite from the exact release commit.
4. Run the read-only production audit from the exact release commit. Any unsupported format blocks the migration.
5. Create an on-demand Cloud SQL backup and record its identifier and successful completion.
6. Confirm the migration command, application rollout, health checks, smoke tests, and rollback commands are available in separate terminals.

## Deployment sequence

1. Require approval for the `crucible-api-main` Cloud Build trigger.
2. Merge the reviewed staging-to-main pull request. Confirm the main build is waiting for approval and production remains on the old image.
3. Approve the main build and watch the deployment, pods, service endpoints, build log, and production health endpoint continuously.
4. Wait until the new pod has started and remains unready because production still reports revision `b7e4a1c96d38`.
5. Run the read-only audit one final time.
6. Apply `DB_CONF_FILE=.env.prod .venv/bin/alembic -x yes-this-is-production=true upgrade 3f2b6a9c1d84` from the exact main release commit.
7. Confirm Alembic completes successfully and production reports revision `3f2b6a9c1d84`.
8. Confirm the new pod becomes ready and the rolling deployment removes the old replica.
9. Confirm `/health/ready` reports the expected main commit, branch, and schema revision.
10. Run authenticated smoke tests for singleton reads, default collections, owner affiliation, created and updated sorting, cursor pagination, facets, anchored pages, and one controlled resource mutation.
11. Confirm the mutation leaves creation time unchanged and advances modification time in canonical UTC.
12. Remove the temporary build-approval requirement after the release is stable.

## Monitoring

- Watch Cloud SQL CPU, active connections, lock waits, transaction latency, and error logs.
- Watch API request latency, 5xx responses, readiness, service endpoints, and pod restarts.
- Record migration start, commit, new-pod readiness, old-pod removal, and smoke-test completion times.

## Abort and rollback

- Audit failure: do not start the migration.
- Backup failure: do not start the migration.
- New pod does not start and wait unready on the old revision: do not start the migration.
- Alembic failure before commit: PostgreSQL rolls back the transaction. Keep the old pod serving and roll back the application deployment.
- New API failure after migration commit: keep the release under active control, run `alembic downgrade b7e4a1c96d38`, restore the previous image, and verify health before ending the release operation.
- Backup restore is the last resort. Writers continuing during the online migration make a restore materially more disruptive than a transactional Alembic downgrade.
