# Hub authentication schema rollout

Status as of 2026-09-28: PR #286 merged and staging deployed at `4910cc8`. Production migration to `e92a6b1c730f` completed with user approval in 2.45 seconds. PR #287 promotes staging to main; the production feature image and authentication activation remain pending.

Both environments completed the migration sequence with explicit approval. Staging's index-only migration took 1.38 seconds. Production's full two-revision migration took 2.45 seconds through the local SQL proxy after verifying database `crucible`, backup/PITR readiness, and absence of long transactions. All five tables and four valid, ready expiry indexes were verified. Exact revision checking was restored, the existing production image rolled successfully, and public readiness returned 200. Production retains its `351f8ef` image until PR #287 is merged.

## Verified state on 2026-09-28

| Environment | Image | Actual schema | Expected schema | Pod readiness |
| --- | --- | --- | --- | --- |
| Main | `351f8ef` | `e92a6b1c730f` | `e92a6b1c730f` | 200 |
| Staging | `4910cc8` | `e92a6b1c730f` | `e92a6b1c730f` | 200 |

Both deployments have one replica, a rolling update strategy, `maxUnavailable: 0`, and `maxSurge: 25%`. Both checked-in readiness settings now expect `e92a6b1c730f`. Do not repeat completed migrations or restore the old expected revision.

The user confirmed sign-in, logout, and fresh sign-in using the Tailscale Hub against staging, and reported existing API clients working. Automatic refresh appears to work by user observation; no independent log verification was performed. Staging authentication was first enabled temporarily with user approval of the documented ingress limitations. From 2026-10-12 the checked-in staging configuration keeps it enabled with the callback `http://localhost:3000/hub/auth/callback`, so builds no longer reset it. Developers run the Hub on port 3000 and open `http://localhost:3000`, forwarding the port (for example `ssh -L 3000:localhost:3000 <workstation>`) when the Hub runs on a remote machine. `HUB_AUTH_CALLBACK_URI` accepts a comma-separated list of exact callbacks; staging also lists the maintainer's Tailscale-served development Hub, which loads much faster than a Vite dev server through an SSH tunnel. Production stays disabled; this acceptance does not extend to it.

## Completed staging recovery

Staging was first recovered by matching readiness to `d81c3a4f62b0`, then migrated to `e92a6b1c730f` using the sequence below. Exact revision checking was restored and the existing image rolled successfully. These recovery and migration steps are complete; do not repeat them. The feature image and authenticated API-key rollout smoke tests remain separate.

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

### Deferred ingress changes

Decision recorded 2026-09-28: defer direct-backend bypass and proxy-to-API transport changes. Existing API access, client addresses, routing, authentication behavior, and performance must not be affected. Do not change load balancers, firewall rules, source-IP policy, proxy topology, or add per-request application processing as part of this deferred work. Reconsider only a separately reviewed, minimal change with compatibility and performance evidence; none has been established yet.

This is a work deferral, not acceptance that the ingress is secure. Nginx-only limits do not prevent direct-backend bypass, and proxy-to-API transport protection remains unverified. The private Tailscale pilot may continue. Public Hub authentication stays disabled until these protections are implemented and verified or the user explicitly accepts the documented residual risks. No infrastructure or runtime change is authorized by this decision.

### Other activation requirements

Schema deployment does not release the Hub. Production activation also requires protected auth ingress without a direct-backend bypass, registered ORCID redirects, the Hub Cloud Run runtime identity and secret grant, the `/hub` proxy route, and deployed browser checks. The pilot uses `HUB_AUTH_SESSION_MODE=single-instance` with process-local sessions, one minimum and one maximum Cloud Run instance, best-effort affinity, and no traffic splitting. Redis and a Hub-owned session database are deferred. Users may need to sign in again after restart, replacement, or misrouting. The API token schema and cleanup are still required. Coordinate Hub changes with its agent; this rollout does not authorize edits to that repository.
