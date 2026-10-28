# Self-hosting Phase 2: proposed plan

Status: proposal, written 2026-10-28, not approved. It records the plan as proposed after the research in `docs/self_hosting_phase2_research.md`, so the discussion can continue from here. Decisions still open are marked.

## Goal

One storage interface in the code for every byte operation (signed upload, signed download, replace, delete, record files), with the vendor-specific parts inside adapters. A small lab runs an object store in the Compose bundle; the Foundry keeps its data in Google Cloud Storage and keeps running unchanged.

## Proposed change to the design

`docs/self_hosting_design.md` (Decisions, Storage) says the Foundry reaches GCS through its S3-compatible XML API with HMAC keys, so a single S3 code path serves everyone. The research found gaps on that path:

- S3 write preconditions (`If-Match`, `If-None-Match`) are silently ignored by GCS, and multipart uploads accept no preconditions; Crucible's replace and delete safety relies on preconditions (GCS generations today).
- Pinning a presigned download to one object version is unverified through the S3 API; downloads, file ETags, and replace depend on it.
- HMAC keys are long-lived secrets, where the current upload signer needs none, and a common organization policy blocks creating them.
- Projects with many GCS users keep a native GCS adapter beside S3 (GitLab, Sentry, Label Studio, DVC); projects relying on the compatibility layer reported breakages.

Decided 2026-10-28: one S3 path for everyone, the Foundry included, reached through a smooth transition that proves it first. Most of the gaps above stop mattering once the S3 path needs neither preconditions nor version pinning (every upload goes to a new unique key, the database decides what is current, and the API does its own cleanup); what remains is the HMAC key and the unverified behaviors, which a test answers.

1. The storage interface comes first, with today's Google code behind it as the Google adapter, so the Foundry runs exactly as now while the S3 path is built.
2. Once the S3 adapter and `storage check` exist, the check runs against a disposable GCS bucket with an HMAC key for a dedicated service account (the user creates both, after checking that the LBL organization does not block service account keys or restrict storage authentication types).
3. If it passes, staging switches to the S3 adapter by setting and runs the crucible-tests suite, then production; afterwards the Google adapter and the `google-cloud-storage` dependency are removed.
4. If it fails, or policy forbids HMAC keys, the Google adapter stays permanently behind the same interface.

The trade-off accepted: one code path that the Foundry's production traffic also exercises, at the cost of a long-lived HMAC key pair (kept as a secret and rotated) and of boto3 upgrades that could break GCS, which the staging check catches.

## Design points

- Signed multipart uploads already write every file to a new key, `{dataset_mfid}/_files/{id}`, and never overwrite one, so the S3 adapter needs neither object versioning nor conditional writes: the key identifies the content. The legacy in-place keys (`{dataset_mfid}/{filename}`) exist only on GCS.
- Retiring a replaced object becomes a database-tracked deletion after a grace period, done through the adapter, instead of the GCS-only `customTime` lifecycle rule. The Google adapter can keep using `customTime` until the new path is proven.
- The adapter exposes what the routes need: start, sign parts, list parts, complete and abort a multipart upload; object metadata (size, checksum, version token); a signed download with a filename and disposition; delete pinned to a version where the store has one; and record file writes.
- Presigned URLs sign the host name, so the S3 adapter signs with the address clients reach (for the bundle, the public address of the store), not the internal service name.
- boto3 is configured with `request_checksum_calculation` and `response_checksum_validation` set to `when_required`, path-style addressing, and an explicit endpoint, which many S3-compatible stores need.
- The API asks little of the store, so any S3-compatible store fits: signed URLs (SigV4) for download and for uploading parts, multipart start, list parts, complete and abort, HEAD for size and checksum, and delete. Object versioning and conditional writes are not needed, and a per-part checksum is used where the store supports it. Which store runs is the deployer's choice; the bundled one is a default, left out with a Compose profile when a lab has its own (agreed 2026-10-28).
- The API cleans up after itself instead of relying on store-side lifecycle rules, which every store configures differently: a scheduled job, tracked in the database, aborts abandoned multipart uploads and deletes retired objects after their grace period. A deployer only points Crucible at a bucket (agreed 2026-10-28).
- `python -m app.cli storage check` runs a real round trip against the configured store (a small multipart upload through signed part URLs, a signed download, a delete) and reports what works, so a deployer learns at once whether their store fits. CI runs it against SeaweedFS and a second store (Garage or Versity) to keep the adapter portable (agreed 2026-10-28).
- Azure Blob Storage is not S3-compatible and would need its own adapter or an S3 gateway; with one interface in the code that is one more adapter, nothing else.
- Per-part CRC32C: GCS enforces it through the signed `x-goog-hash` header; the S3 adapter uses `x-amz-checksum-crc32c` where the store supports it, and the server checks the combined checksum at completion either way.

## Bundled object store

SeaweedFS (Apache-2.0, very active, one container configured from the environment), with Versity S3 Gateway as the runner-up (plain files on the host filesystem, easiest to back up). MinIO's community edition is no longer maintained; Garage lacks versioning and has a fragile metadata store on one node. Open decision 2.

## Pull requests, in order

1. The storage interface with the Google adapter behind it: downloads, retiring, record files, and object metadata move into the adapter, and the three Google credential paths are consolidated. No behavior change for the Foundry; the existing tests and the staging checks prove it.
2. The S3 adapter, the setting that chooses the adapter, and `python -m app.cli storage check`, tested in CI against real SeaweedFS and Garage or Versity containers, not only against fakes.
3. The cleanup job: aborting abandoned multipart uploads and deleting retired objects after their grace period, through the adapter, replacing the GCS lifecycle rules for the S3 path.
4. The Compose bundle gains SeaweedFS: a bucket created on first start, an address clients reach for presigned URLs, CORS for browser uploads, the install guide, and an upload and download step in the smoke test.
5. Deleting a file or a dataset also deletes its stored objects, through the adapter and after a grace period, behind a setting, staging first. Today both remove database rows only. Open decision 3: part of Phase 2, or its own project.
6. The legacy upload routes (`/upload/initiate`, `/upload/complete`, the downscoped upload token) are deprecated, after checking with the nano-crucible session which client versions still use them.

Each pull request goes to staging, is checked in the staging pod and by the crucible-tests suite, and the Foundry switches nothing until the S3 path is proven.

## Also found, to fix along the way

- SHA-256 is client-claimed and never verified by the server; decide whether the server verifies it (reading the object once after upload) or the field is documented as a client claim.
- Stale docs: `docs/file_object_cleanup_followup.md` and `docs/codebase_review_2026-10.md` say hard delete removes objects; `docs/multipart_upload_api.md` names an old staging bucket and a default `UPLOAD_BUCKET`.
- `MULTIPART_UPLOAD_BUCKET` and other multipart settings are still read from the environment directly instead of through the settings.

## Requests from mf-crucible-deploy (independent of the storage decisions)

Received 2026-10-28; not started, waiting for the user's go.

1. One image per version, built on `vX.Y.Z` tags into one Artifact Registry repository by a dedicated service account with writer access to that repository only; nothing environment-specific in the image (`BRANCH_NAME` read at run time from the ConfigMap), so staging tests the exact digest production gets. The per-branch builds stay until the cut-over. API side: a tag-triggered build configuration and moving `BRANCH_NAME` to run time. The user creates the service account, repository, and trigger.
2. `python -m app.cli settings check`: takes the rendered Kubernetes YAML (Deployment and ConfigMap), resolves `configMapKeyRef` mappings, runs the startup settings check, checks the form of Secret Manager paths and, given credentials, that each version exists without reading the value, and exits non-zero on failure. It replaces `tests/test_k8s_pinned_settings.py` once the manifests leave this repository.

Until the cut-over, any `k8s-conf/` change in this repository is mirrored into `foundry/` in mf-crucible-deploy.

## Open decisions

1. Decided 2026-10-28: one S3 path, proven on a test bucket and on staging, with the Google adapter as the fallback (see above).
2. SeaweedFS as the bundled store.
3. Whether deleting stored objects on file and dataset deletion is part of Phase 2.
