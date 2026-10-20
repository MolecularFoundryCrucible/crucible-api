# Release runbook: staging to main, October 2026

## Status

Draft (2026-10-13). Nothing in this runbook has been executed on production. Each step marked **approval** needs explicit user approval at the time it runs.

## Scope

`main` is at `ff7b369` with database schema `b7e2d9c4a1f3`. `staging` is 63 commits ahead (at `d12b998` when written) and expects schema `f3b7c2a91d04`. The release ships everything under `## [Unreleased]` in `CHANGELOG.md`, notably:

- Signed multipart uploads (`/datasets/{dataset_mfid}/upload/multipart/...`), one upload bucket per environment, and file replacement (`POST /files/{file_mfid}/upload/multipart/initiate`, `etag`, structured `filename_exists`).
- Storage location confinement for downloads and ingestion, `disposition=attachment|inline` on download links, and `PATCH /files` limits for GCS files.
- The downscoped legacy parallel-upload token (`{dataset_mfid}/` only, excluding `{dataset_mfid}/_files/`).
- Platform contributors creating datasets and samples in any project; structured 409s; `GET /users/{user_ref}/projects` parity; Hub step-up authentication and unique user emails.

The nano-crucible release that drops all fallbacks to the current production API is gated on this promotion, so multipart must be enabled on production as part of it.

## Verified prerequisites

| Check | Result (2026-10-13, read-only) |
|---|---|
| `migrations/data/20261012_audit_user_emails.py` on production | 3,068 users, 0 shared emails, 1 blank email (not blocking; migration `e5a1c8d93f27` only refuses duplicates) |
| `migrations/data/20261013_audit_file_storage_paths.py` on production with `UPLOAD_BUCKET=MULTIPART_UPLOAD_BUCKET=mf-storage-prod` | 245,506 records: 239,509 pass the location rule, 5,997 have no path (backfilled on download), 0 fail; 0 keys under `{dataset_mfid}/_files/` |
| Pending production migrations | `b72f6c903a18`, `c83a7d014b29` (multipart), `d94e1f2a7b05` (Hub `auth_time`), `e5a1c8d93f27` (unique email index, built concurrently), `f3b7c2a91d04` (replace sessions); all additive |
| Cloud SQL `crucible-db` (shared by `crucible` and `crucible-staging`) | Automated daily backups (7 retained) and point-in-time recovery with 7 days of transaction logs are enabled |
| Ingestion consumers | Production runs `ingestion-cloud-consumer` `main:c3abb8d` with crucible-ingestion `5bf88f8`; its default bucket mapping already resolves `mf-storage-prod/{dataset_mfid}/_files/...` and it classifies by `file_name`. No consumer change is needed. |

Re-run both audits on release day if more than a few days have passed.

## Phase 1: infrastructure (before release day)

All **approval**. None of these change production behavior on their own while `MULTIPART_UPLOAD_ENABLED` is false.

1. Production signer service account, mirroring staging's `crucible-mpu-signer-staging`. Done 2026-10-13: `crucible-mpu-signer-prod` exists with `roles/storage.objectAdmin` on `mf-storage-prod` and `roles/iam.serviceAccountTokenCreator` for the pods' Workload Identity principal.

   ```bash
   gcloud iam service-accounts create crucible-mpu-signer-prod --project=mf-crucible \
     --display-name="Crucible multipart signer (production)"
   gcloud storage buckets add-iam-policy-binding gs://mf-storage-prod \
     --role=roles/storage.objectAdmin \
     --member="serviceAccount:crucible-mpu-signer-prod@mf-crucible.iam.gserviceaccount.com"
   gcloud iam service-accounts add-iam-policy-binding \
     crucible-mpu-signer-prod@mf-crucible.iam.gserviceaccount.com --project=mf-crucible \
     --role=roles/iam.serviceAccountTokenCreator \
     --member="principal://iam.googleapis.com/projects/776258882599/locations/global/workloadIdentityPools/mf-crucible.svc.id.goog/subject/ns/default/sa/default"
   ```

2. `mf-storage-prod` lifecycle: keep the two existing versioning rules and add abort-incomplete-multipart and the replace retirement rule. The bucket has versioning and Autoclass; both actions are compatible with Autoclass. A retired object deleted by the `daysSinceCustomTime` rule becomes a noncurrent version and is kept another 30 days by the existing rule. Before applying, confirm no object in the bucket has `customTime` set (the API never set it before this release).

   ```json
   {"rule": [
     {"action": {"type": "Delete"}, "condition": {"isLive": false, "numNewerVersions": 15}},
     {"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 30}},
     {"action": {"type": "AbortIncompleteMultipartUpload"}, "condition": {"age": 8}},
     {"action": {"type": "Delete"}, "condition": {"daysSinceCustomTime": 7}}
   ]}
   ```

   ```bash
   gcloud storage buckets update gs://mf-storage-prod --lifecycle-file=lifecycle-prod.json
   gcloud storage buckets describe gs://mf-storage-prod --format="yaml(lifecycle_config)"
   ```

3. CORS for browser multipart uploads (PUT, exposing `x-goog-hash` and `ETag`) is only needed once the Hub runs against production. Hub authentication stays disabled on production, so defer it; nano does not need CORS.

4. Optional: enable soft delete on `mf-storage-prod` (it is 0 today) for an extra recovery window on any deletion.

## Phase 2: configuration PR to staging (before release day)

Branch `chore/prod-multipart-config` makes these changes. Before it, `k8s-conf/deployment-k8s-conf-main.yaml` did not wire the multipart variables. The PR:

- Adds to `k8s-conf/configmap-main.yaml`: `MULTIPART_UPLOAD_ENABLED: "true"`, `MULTIPART_UPLOAD_BUCKET: "mf-storage-prod"`, `MULTIPART_UPLOAD_SIGNER: "crucible-mpu-signer-prod@mf-crucible.iam.gserviceaccount.com"`, `MULTIPART_UPLOAD_PROJECT: "mf-crucible"`.
- Wires those four keys as `configMapKeyRef` env entries in `k8s-conf/deployment-k8s-conf-main.yaml`, matching the staging deployment.
- Extends `tests/test_storage_buckets.py` so the main configmap asserts the multipart wiring and `MULTIPART_UPLOAD_BUCKET == UPLOAD_BUCKET`.

Merging it to `staging` changes nothing on production; it only takes effect with the promotion.

## Phase 3: release day

Announce a short maintenance window to API users. Pause other merges to `main` and `staging` and to `ingestion-cloud-consumer` `main` (whose merges deploy production consumers immediately). Record the current production image, configmap, schema revision, and baseline health.

1. **Approval.** Confirm backup and recovery readiness for Cloud SQL instance `crucible-db` (point-in-time recovery enabled, or an on-demand backup: `gcloud sql backups create --instance=crucible-db --project=mf-crucible --description="pre staging-to-main 2026-10"`). Check for long-running transactions on database `crucible`.
2. **Approval.** Relax readiness on the running image so the old code keeps serving while the schema moves: set `EXPECTED_DB_REVISIONS` to an empty string in the live `crucible-api-config-main` configmap and roll the existing `crucible-api-main` deployment. Verify the serving pod has the empty value and `/api/v3/health/ready` returns 200. Every pending migration is additive, so the old code runs against the new schema.
3. **Approval.** Migrate production to the explicit target, verifying the database name first:

   ```bash
   DB_CONF_FILE=.env.prod .venv/bin/alembic -x yes-this-is-production=true current
   DB_CONF_FILE=.env.prod .venv/bin/alembic -x yes-this-is-production=true upgrade f3b7c2a91d04
   DB_CONF_FILE=.env.prod .venv/bin/alembic -x yes-this-is-production=true current
   ```

   Verify the revision is `f3b7c2a91d04`, the `upload_sessions` multipart and replace columns exist, and `ux_user_email_lower` is valid. The email index is built concurrently and waits for existing transactions.
4. **Approval.** Merge the `staging` to `main` promotion PR. Cloud Build applies `configmap-main.yaml` (restoring `EXPECTED_DB_REVISIONS=f3b7c2a91d04` and enabling multipart) and rolls the new image. Watch the build and the rollout; confirm `/api/v3/health/ready` reports `git_commit` of the merge, schema `f3b7c2a91d04`, status 200.

## Phase 4: smoke tests

Use an approved test project and a clearly named throwaway dataset; delete it afterwards.

- `GET /health/ready`, `GET /account/profile`, `GET /projects?limit=1`, `GET /users/{user_ref}/projects?sort=title`.
- An existing file: `GET /files/{id}` returns `etag` and matching `ETag` header; `download_link` default (attachment) and `?disposition=inline`; a bad disposition returns 422.
- Legacy upload (`/upload/initiate`) of a small file, and a legacy parallel upload, which also exercises the downscoped token on production.
- New multipart upload with nano 3.3.0 (dev build) to `mf-storage-prod/{dataset_mfid}/_files/{upload_id}`; download and compare SHA-256.
- Ingestion of a small PNG uploaded through the new flow (auto-detect) and one through the legacy route; both reach a terminal success with a thumbnail. Watch `ingestion-consumer-main-small` logs.
- Replace the multipart test file with `If-Match`; confirm same MFID, new content, and the old object gets `customTime`. Confirm a stale `If-Match` returns 412 and an existing filename on create returns 409 `filename_exists`.
- Review the last 30 minutes of API logs for 5xx and tracebacks (Cloud Logging tags stderr INFO lines as ERROR; count status codes from the request lines instead).

## Rollback

The migrations are additive and the old code runs on the new schema, so roll back the application, not the database:

1. Re-run the build of `ff7b369` (or `kubectl set image deployment/crucible-api-main ...:ff7b369`) and set `EXPECTED_DB_REVISIONS` to an empty string (or `f3b7c2a91d04`) in the live configmap, because `ff7b369`'s checked-in configmap expects `b7e2d9c4a1f3` and would fail readiness.
2. Set `MULTIPART_UPLOAD_ENABLED` to `false` if only multipart misbehaves; initiation stops with 503 while status, completion, and abort keep working, and the released nano reports it clearly.
3. Do not downgrade migrations unless a migration itself is faulty; `e5a1c8d93f27`'s downgrade drops the email index concurrently, and the multipart downgrades drop session data.

## After the release

- Tell nano-crucible that production runs the staging contract so it can publish the release; tell crucible-hub to re-pin against production.
- Watch production API and consumer logs for 5xx and ingestion failures for a day.
- Update this runbook's status with what ran, timings, and the verified state, and add it to `docs/README.md`.
- Follow-ups not in this release: per-environment pod identities, a keyless download signer, CORS when the Hub moves to production, letting editors abort stale replace sessions, staging consumers for the medium and large tiers.
