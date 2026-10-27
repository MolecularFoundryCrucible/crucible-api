# Self-hosting Phase 1: production release checklist

Status: prepared 2026-10-27. The release is the user's merge of `staging` into `main`; Cloud Build then deploys production.

## What ships

Production runs `1559c56` (pull request #353, 2026-10-22). `main` has no commits that `staging` lacks. The release carries ten pull requests, each reviewed on its own, deployed to staging, and verified there with checks inside the pod and the crucible-tests suite:

| PR | Task | Effect on the Foundry |
|---|---|---|
| #358 | Pin the Foundry's settings in the ConfigMaps | adds keys only (values the code used to default) |
| #359, #360 | Settings and secrets read explicitly with pydantic-settings | same values, read from the pinned keys |
| #362 | Feature switches, explicit storage settings, startup check | all features on; startup refuses missing settings |
| #363 | Settings read on first use, API address settings, container port | port 7777 from `PORT`, 4 workers as before |
| #364 | Schema baseline and `db upgrade` | ships migration files in the image; no migration runs |
| #365 | First-administrator command | operator command only |
| #366 | Settings grouped by area, one way per secret | same values; Hub secrets checked at startup |
| #367 | Docker Compose bundle and install guide | nothing deployed |
| #368 | PostgreSQL tests and smoke test in CI | CI only; Alembic no longer disables other loggers |

No Alembic revision is added; production's database is already at `b8f2d6e41c07`, which `EXPECTED_DB_REVISIONS` pins.

## Why the Foundry is unaffected

- Every value the code used to assume is in `k8s-conf/configmap-main.yaml` (#358), and `tests/test_k8s_pinned_settings.py` runs the real production ConfigMap and Deployment through the new settings code in CI: every variable is recognized, none is given two ways, the startup check passes, and key values resolve exactly (database, Hub, buckets, RabbitMQ, MQTT, proposal service, expected revision). The same test covers the cleanup CronJob.
- Deploy order (`cloudbuild.yaml`): build and push the image with the commit tag and `latest`; apply the ConfigMap; apply the Deployment (image `latest`, which is already the new build); pin the Deployment to the commit tag; patch the cleanup CronJob. New pods start with the new code and the new ConfigMap together. Pods read their environment only at start, so running old pods are unaffected by the new ConfigMap keys while the rolling update proceeds.
- Old and new pods share the same database schema, API keys, session cookie signing key, and cursor signing key, so a request or cursor issued by one is accepted by the other during the rollout.

## After the deploy (first hour)

1. Cloud Build succeeds; `kubectl rollout status deploy/crucible-api-main` completes; every pod runs the new commit tag, with no restarts.
2. In a pod: `/health/ready` reports `status ok` and `schema_state current` (port 7777); `check_settings()` passes; every group resolves to the production values; the nine secrets and both Hub secrets resolve; the Hub login configuration loads; cursor keys are `v1`.
3. Logs: no `ConfigurationError` or tracebacks.
4. The Hub at `https://crucible.lbl.gov/hub/` signs in and lists resources (one manual sign-in).
5. The next hourly cleanup run on the new image succeeds (checked once, by request).
6. crucible-tests against production, if the user wants it (it writes and removes test records).

## Rollback

Redeploy the previous image: `kubectl set image deployment/crucible-api-main crucible-api-container=us-central1-docker.pkg.dev/mf-crucible/crucible-api/crucible-api-main:1559c56` and `kubectl set image cronjob/crucible-hub-auth-cleanup-main cleanup=us-central1-docker.pkg.dev/mf-crucible/crucible-api/crucible-api-main:1559c56`, or revert the merge on `main` and let Cloud Build redeploy. The old code ignores the added ConfigMap keys, and no database change ships, so rolling back needs no data step.

## Final review

A whole-release review (three independent reviewers, production risk only) of `main..staging` ran before the release; findings and dispositions are recorded below.

The reviewers reported one high, one medium and seven low items; one reviewer found nothing. Each was checked against `main`, `staging` and `k8s-conf`:

| Finding | Verdict | Disposition |
|---|---|---|
| (high) `RECORD_BUCKET` lost its `crucible-records` default, so production would stop writing record files | Wrong | `main` already reads `RECORD_BUCKET` with no default, and `RECORD_BUCKET: "mf-record-prod"` is in `configmap-main.yaml` and wired in `deployment-k8s-conf-main.yaml` on both branches. Nothing to do. |
| (medium) Secrets are no longer fetched when a worker starts, so a bad secret passes readiness and fails on live requests | Real, narrow | On `main`, importing the app fetched `PGDB_PW`, `CRUCIBLE_SECRET_KEY` and `RABBITMQ_DEFAULT_PW`; the others were already fetched on first use. Now only `PGDB_PW` is exercised before traffic (by `/health/ready`). The release keeps the exact secret versions production reads today, so this matters only if a secret changes later. Covered by step 2 above (every secret resolves in a pod). Follow-up: resolve the secrets every request needs at startup. |
| (low) `GCS_SA_VERSION` and the Hub and ORCID secrets use `versions/latest` while values are cached per worker | Real, unchanged | `main` used the same `latest` versions. Pinning numeric versions is a ConfigMap change for later. |
| (low) `POST /files/{file_id}/ingest` answers 503 with a structured `detail` and marks the request failed when the broker is down | Intended | In the CHANGELOG. nano-crucible retries 503 (`client.py`, `status_forcelist` 429/502/503/504) and did not retry the old 500, so a broker outage is now retried and the failed attempts are recorded. |
| (low) `require_hub_client` catches `HubSecretUnavailable` but not `ConfigurationError` (500 instead of 503) | Unreachable in production | `ConfigurationError` there comes only from a secret given two ways, which the startup check refuses. Consistency fix for later. |
| (low) Secret Manager values are not stripped of a trailing newline | Unchanged | Same as `main`; the Hub secrets reject a newline explicitly. |
| (low) Cloud Build rolls the Deployment twice (apply with `latest`, then pin the commit tag) | Unchanged | `cloudbuild.yaml` is not in the release. Both rollouts run the new image. Fix with the Foundry deploy repository step. |
| (low) Multipart upload settings are read from the environment on each call while the rest are read once | Real, harmless | Production does not change these at runtime. Fold into `StorageSettings` later. |
| (low) Disabled features answer 501 while disabled multipart upload and Hub sign-in answer 503 | Intended | Documented in `docs/error_responses_followup.md`; every production feature is on. |

Verdict: nothing blocks the release. Rollout, side-by-side pods, the Dockerfile (port 7777 from `PORT`, 4 workers, gunicorn as PID 1), the CronJob and rollback were confirmed by all three reviewers.
