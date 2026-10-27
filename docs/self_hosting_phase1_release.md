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
