# Hub authentication schema rollout

Status: Proposed sequence, live changes require explicit approval.

Staging completed this sequence on 2026-09-28 with user approval. Revision `e92a6b1c730f` was applied through the local SQL proxy in 1.38 seconds after verifying the database name, backup/PITR status, and absence of long transactions. All five Hub tables and four valid expiry indexes were verified. Exact revision checking was restored and the rolling update completed on the existing `c8baeec` image. Public staging readiness returned 200 at the target revision; production remained healthy at `4a7d2c9e1f60`. PR #286 has not been merged and no new API image was deployed. The production migration remains pending separate approval.

## Verified state on 2026-09-28

| Environment | Image | Actual schema | Expected schema | Pod readiness |
| --- | --- | --- | --- | --- |
| Main | `351f8ef` | `4a7d2c9e1f60` | `4a7d2c9e1f60` | 200 |
| Staging | `c8baeec` | `d81c3a4f62b0` | `4a7d2c9e1f60` | 503 |

Both deployments have one replica, a rolling update strategy, `maxUnavailable: 0`, and `maxSurge: 25%`. Staging's database is reachable; its readiness failure is the exact schema comparison. Do not merge PR #286 with unchanged staging readiness configuration.

## Immediate staging recovery

Completed with user approval on 2026-09-28: staging's expected revision was set to `d81c3a4f62b0` and its existing `c8baeec` image rolled successfully. The replacement pod returned healthy readiness. No database migration or main deployment change was performed. The feature branch's staging ConfigMap now matches; the deployed source branch still needs the same correction before another automated build can restore the stale value. Authenticated API-key smoke tests remain separate.

## Migration sequence

Use staging first, then repeat the approved sequence for main. Pause competing release workflows for the affected environment. Record its current image, configuration, actual revision, and baseline health before proceeding.

1. Temporarily set `EXPECTED_DB_REVISIONS` to an empty string for the affected API deployment and complete a rolling restart of the existing image. The existing readiness implementation still requires a successful database query, but skips exact revision matching. Verify every serving pod has the temporary setting before migrating. This is an explicit, time-bounded reduction in schema validation, not a permanent configuration.
2. Keep `HUB_AUTH_ENABLED=false`. Check database backup/recovery readiness and inspect long-running transactions. With separate migration approval, run the feature branch's Alembic upgrade to the explicit target `e92a6b1c730f` through the approved SQL proxy. Verify the database name before execution; production additionally requires Alembic's `-x yes-this-is-production=true` guard. Set a bounded lock timeout for the migration session so lock contention fails instead of waiting indefinitely.
3. Verify the single recorded revision is `e92a6b1c730f`, all five Hub tables exist, and all four expiry indexes are valid. Main adds the five tables and indexes; staging already has the tables and only needs the expiry-index revision. Creating foreign keys may briefly lock referenced tables. Concurrent index builds avoid blocking ordinary writes but can wait for existing transactions and consume I/O.
4. Update the release configuration to expect exactly `e92a6b1c730f`, deploy the reviewed feature image with Hub login still disabled, and wait for the rolling update to finish. Ensure Cloud Build applies the reviewed configuration rather than a stale expected revision. Verify all serving pods use the exact target setting and report healthy readiness.
5. Smoke-test existing API-key authentication and representative reads and writes using approved test resources. Confirm no elevated error rate. Leave Hub cleanup suspended and Hub login disabled until their separate activation gates pass.

The temporary configuration must be preserved if a build could run during step 2. Prefer completing the migration before the feature release merge/build, with the final revision pin included in that release. A ConfigMap edit alone does not update environment variables in existing pods.

Do not set `EXPECTED_DB_REVISIONS=old,new`: this means the database must contain both revision rows, not that either is accepted. Do not switch readiness to liveness, disable database connectivity checks, delete all pods, or migrate before the bridge rollout completes.

## Failure handling

If the bridge rollout cannot complete, stop before migrating. If a migration fails, leave the compatible existing image serving, inspect the actual schema, and repair only the failed migration's artifacts. The concurrent-index revision may partially commit; follow the index inspection and retry instructions in [Hub authentication operations](hub_auth_operations.md#expired-record-cleanup). Do not stamp a failed migration or blindly rerun it.

If the application rollout fails after a successful migration, restore the previous compatible image while retaining the new tables and the verified target revision setting. Do not use a blind deployment undo that restores the stale expected revision. Do not downgrade the Hub tables: staging already contains local-test sessions, and future activation will create live sessions. Restore exact readiness validation against the verified actual revision even when the feature release is postponed.

## Separate production activation gates

Schema deployment does not release the Hub. Production activation also requires protected auth ingress without a direct-backend bypass, registered ORCID redirects, the Hub Cloud Run runtime identity and secret grant, the `/hub` proxy route, and deployed browser checks. The pilot uses `HUB_AUTH_SESSION_MODE=single-instance` with process-local sessions, one minimum and one maximum Cloud Run instance, best-effort affinity, and no traffic splitting. Redis and a Hub-owned session database are deferred. Users may need to sign in again after restart, replacement, or misrouting. The API token schema and cleanup are still required. Coordinate Hub changes with its agent; this rollout does not authorize edits to that repository.
