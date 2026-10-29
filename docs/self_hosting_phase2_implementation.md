# Self-hosting Phase 2: storage implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: written 2026-10-28 for review; not started.

**Goal:** One storage interface for every byte the API stores, with a Google adapter (today's code, unchanged behavior) and an S3 adapter, and SeaweedFS in the Compose bundle, so a self-hosted installation stores and serves files.

**Architecture:** A small `ObjectStore` protocol in `app/storage/` with two implementations, chosen by `STORAGE_BACKEND`. The routes and the multipart service talk only to the protocol. The API does its own cleanup of abandoned uploads and retired objects, and `python -m app.cli storage check` proves a configured store end to end. In the bundle, Caddy forwards `/<bucket>/...` to SeaweedFS unchanged, so presigned URLs use the site's own address.

**Tech Stack:** Python 3.11, FastAPI, SQLModel and Alembic on PostgreSQL 17, google-cloud-storage (Google adapter), boto3 (S3 adapter), SeaweedFS 4.51 (`chrislusf/seaweedfs`), Caddy 2.11, Docker Compose.

**Spec:** `docs/self_hosting_phase2_plan.md` (decisions), with `docs/self_hosting_phase2_research.md` (research, client constraints) and `docs/self_hosting_design.md` (Storage, Phases).

## Global Constraints

- The Foundry runs unchanged until the GCS test in Task 7 passes and the user switches staging: `STORAGE_BACKEND` defaults to `gcs`, and every Google code path keeps its behavior.
- Objects the API stores are recorded with `storage_backend = "gcs"` whatever the store, because every released client treats any other value as a catalog-only record. Document `"gcs"` as the historical name for "stored by the API".
- New uploads always go to a new key, `{dataset_mfid}/_files/{upload_id}`; nothing on the S3 path overwrites a key, so the S3 adapter needs neither object versioning nor conditional writes.
- boto3 clients: `signature_version="s3v4"`, `s3={"addressing_style": "path"}`, `request_checksum_calculation="when_required"`, `response_checksum_validation="when_required"`, region from `S3_REGION` (default `us-east-1`). A second client with `endpoint_url = S3_PUBLIC_URL` is used only for presigning.
- Part checksums on S3 are signed into the part URL (`ChecksumCRC32C`), so the client must send the returned `x-amz-checksum-crc32c` header; nano-crucible and the Hub already forward the part headers the API returns.
- SeaweedFS image `chrislusf/seaweedfs:4.51` (at least 4.45); never set `S3_EXTERNAL_URL` in the bundle.
- No test touches real GCS; S3 integration tests run against real SeaweedFS and Versity S3 Gateway containers in CI.
- Each task is one pull request to `staging`, with a `CHANGELOG.md` entry under `Unreleased` for anything operators or clients see. Markdown is not hard-wrapped; no em dashes.
- Deleting stored data on file and dataset deletion stays out of scope (deferred); the cleanup job only aborts abandoned uploads and deletes objects retired by a replacement.

## Review Focus

1. A client reaching the server by a different address than `S3_PUBLIC_URL` (for example `http://labbox:8080` against `http://localhost:8080`): every presigned URL fails with SignatureDoesNotMatch. Task 2's settings check warns when `S3_PUBLIC_URL` and `API_PUBLIC_URL` differ in scheme or host, and the guide (Task 6) says to use one address.
2. Released clients send `Authorization: Bearer <API key>` on downloads; S3 rejects a presigned URL with a second auth mechanism. Task 6's compose smoke test downloads with that header present and must succeed because Caddy strips it.
3. A part re-signed after a 403 or expiry, uploaded twice: the second upload replaces the first, and completion uses the listed part. Task 2's integration test uploads one part twice and completes.
4. A file whose size is an exact multiple of the part size, and a 1-byte file: no empty trailing part, one part of 1 byte. Task 2's integration test covers both.
5. The store unreachable at completion: the session stays `uploading` and is retried by the client; the cleanup job aborts it after expiry. Task 3's test covers the abort path.

---

## Order (agreed 2026-10-28)

Task 1a, Task 1b, Task 2, Task 6, Task 3, Task 4, Task 5, Task 7. Task 1 is split so each review stays small, and the bundle (Task 6) comes right after the S3 adapter, so a self-hosted installation stores files as early as possible; the cleanup job, empty files, and the legacy routes matter less in a fresh installation and follow. Task numbers below keep their original names.

## Before the storage work: Hub image from GHCR (done, PR #381)

Small, independent pull request, worth doing first. The Hub now publishes `ghcr.io/molecularfoundrycrucible/crucible-hub:1.7.1` (amd64 and arm64, anonymous pulls, label `gov.lbl.crucible.api.min-version=3.2.0`). Switch the `compose-hub` CI job and the install guide from building the Hub at `v1.7.0` to pulling that image (`HUB_IMAGE` in the Hub's `.env`, then `docker compose pull hub hub-setup`), pinned by digest in CI. Verify with the existing `compose-hub` job.

## Process change after the Foundry cut-over

After mf-crucible-deploy takes over (its Phase 3), staging runs only release images built from `vX.Y.Z` tags, so merging a pull request no longer puts it on staging. Each Phase 2 task needs staging only from Task 6 on (Task 7 needs a GCS test bucket, not staging). Open question for the user and the deploy session: either release Phase 2 work as patch versions, or allow release-candidate tags (`vX.Y.Z-rc.N`) that build images staging may run but production may not. Until the cut-over, merges to `staging` deploy as today, and the deploy session is told after each one.

## File Structure

- `app/storage/__init__.py`: `get_store() -> ObjectStore` (cached, chooses by `STORAGE_BACKEND`).
- `app/storage/base.py`: the `ObjectStore` protocol, `ObjectInfo`, `Part`, `StorageFailure`, `UploadMissing`.
- `app/storage/gcs.py`: `GoogleStore`, built from today's `app/services/multipart_storage.py`, the signing and retiring parts of `app/services/storage_downloads.py`, and the client in `app/utils/gcs_record_utils.py`.
- `app/storage/s3.py`: `S3Store` (boto3).
- `app/storage/crc32c.py`: `crc32c_combine(crc_a: int, crc_b: int, length_b: int) -> int` and base64 helpers.
- `app/services/storage_cleanup.py`: aborting abandoned uploads, deleting retired objects.
- `app/cli/storage.py`: `storage check` and `storage cleanup`.
- `app/services/multipart.py`, `app/api/routes/multipart.py`, `app/api/routes/gcs_storage.py`, `app/api/routes/files.py`, `app/api/routes/datasets.py`, `app/services/storage_downloads.py`, `app/utils/gcs_record_utils.py`: call `get_store()` instead of Google clients.
- `app/config.py` (`StorageSettings`), `app/utils/multipart_config.py`: the new settings; the `MULTIPART_*` variables move into the settings.
- `deploy/compose/`: `seaweedfs` service, Caddy storage route, secrets, guide.
- Removed at the end: `app/services/multipart_storage.py`.

---

### Task 1: Storage interface with the Google adapter behind it

No behavior change for the Foundry; this is the seam every later task uses. Split into two pull requests:

- **Task 1a:** `app/storage/` with the protocol, `GoogleStore`'s multipart methods (`start_upload`, `open_uploads`, `list_parts`, `sign_part`, `finish_upload`, `abort_upload`, `head`, `delete`) and `get_store()`; the multipart service and routes use it; `app/services/multipart_storage.py` is removed; `MemoryStorage` and `tests/test_multipart_storage.py` move to the new names.
- **Task 1b:** signed downloads, retiring (`retire`, `retires_by_lifecycle`), and record files (`put_bytes`, `get_text`) move behind the same interface; `StorageObject.generation` becomes `version: str | None`; the doc corrections.

Each is verified on staging (crucible-tests plus a real upload, replace, and download) before the next starts.

**Files:**
- Create: `app/storage/__init__.py`, `app/storage/base.py`, `app/storage/gcs.py`
- Modify: `app/services/multipart.py`, `app/api/routes/multipart.py`, `app/api/routes/gcs_storage.py`, `app/api/routes/files.py`, `app/api/routes/datasets.py`, `app/services/storage_downloads.py`, `app/utils/gcs_record_utils.py`, `tests/test_multipart_lifecycle.py` (the `MemoryStorage` fake), `tests/test_multipart_storage.py`, `tests/test_storage_downloads.py`, `tests/test_record_bucket.py`
- Delete: `app/services/multipart_storage.py`
- Docs: correct `docs/file_object_cleanup_followup.md` (hard delete does not remove objects today) and replace `docs/multipart_storage_backends.md` with a pointer to this plan.

**Interfaces:**
- Produces, in `app/storage/base.py`:
  - `Part(number: int, size: int, etag: str)` and `ObjectInfo(size: int, crc32c: str | None, version: str)` (frozen dataclasses; `crc32c` is base64, `version` is the GCS generation or the S3 version token, always a string).
  - `class StorageFailure(Exception)` with `code: str`; `class UploadMissing(StorageFailure)`.
  - `class ObjectStore(Protocol)` with `retires_by_lifecycle: bool` and:
    - `start_upload(bucket: str, key: str) -> str` (provider upload id)
    - `open_uploads(bucket: str, key: str) -> list[str]`
    - `list_parts(bucket: str, key: str, upload_id: str, marker: int, limit: int) -> tuple[list[Part], int | None]`
    - `sign_part(bucket: str, key: str, upload_id: str, number: int, crc32c: str, size: int, expires_at: datetime) -> tuple[str, dict[str, str]]`
    - `finish_upload(bucket: str, key: str, upload_id: str, parts: list[Part], crc32c_by_part: dict[int, str]) -> None`
    - `abort_upload(bucket: str, key: str, upload_id: str) -> None` (no error when already gone)
    - `head(bucket: str, key: str) -> ObjectInfo | None`
    - `signed_download(bucket: str, key: str, version: str | None, filename: str, disposition: str, content_type: str | None, expires_in: timedelta) -> str`
    - `put_bytes(bucket: str, key: str, data: bytes, content_type: str) -> None`
    - `get_text(bucket: str, key: str) -> str | None`
    - `delete(bucket: str, key: str, version: str | None) -> None` (no error when already gone)
    - `retire(bucket: str, key: str, version: str | None, retired_at: datetime) -> None` (Google: `customTime`; never called when `retires_by_lifecycle` is false)
- Produces, in `app/storage/__init__.py`: `get_store() -> ObjectStore` (`lru_cache`; `GoogleStore` while only Google exists).
- `StorageObject.generation` in `app/services/storage_downloads.py` becomes `version: str | None`; `GoogleStore` converts to `int` where the Google client needs it.

- [ ] **Step 1:** Rewrite `MemoryStorage` in `tests/test_multipart_lifecycle.py` against `ObjectStore` (string versions, `crc32c_by_part` accepted and recorded); run the lifecycle tests and watch them fail on the old call names.
- [ ] **Step 2:** Add `app/storage/base.py` and `app/storage/gcs.py`; `GoogleStore` holds today's XML-API code, signed downloads (`generate_signed_url` v4 with `generation`, `response_disposition`, `response_type`), `retire` (`custom_time` with `if_generation_match`), and record reads and writes, with the same three credentials as today (impersonated signer for multipart, `GCS_SA` client for the rest). `finish_upload` ignores `crc32c_by_part` (GCS enforces part checksums through the signed `x-goog-hash`).
- [ ] **Step 3:** Point `app/services/multipart.py`, the multipart routes, downloads (`_signed_download`), ingestion path resolution, retirement (`retire_if_eligible` calls `store.retire` when `retires_by_lifecycle`), and `write_record`/`write_metadata` at `get_store()`. Keep `_determine_storage_path` (legacy prefix listing) on the Google client in `gcs_storage.py`; it is Google-only.
- [ ] **Step 4:** Update `tests/test_multipart_storage.py`, `tests/test_storage_downloads.py`, `tests/test_record_bucket.py` to the new names; assertions on signed headers, generation pinning, and `custom_time` stay the same values.
- [ ] **Step 5:** Run the full suite, `CRUCIBLE_TEST_PG_URL` set: `pytest -q`. Expected: everything passes with no test removed except those for deleted functions, which move to `GoogleStore`.
- [ ] **Step 6:** On staging after merge (until the cut-over: automatic), run the crucible-tests suite and one real multipart upload, replace, and download with nano-crucible; tell the deploy session.

### Task 2: S3 adapter, storage settings, and `storage check`

**Files:**
- Create: `app/storage/s3.py`, `app/storage/crc32c.py`, `app/cli/storage.py`, `tests/test_crc32c.py`, `tests/test_s3_store.py` (botocore Stubber), `tests/test_s3_store_integration.py` (marked `s3`, skipped without `S3_TEST_ENDPOINT`), `migrations/versions/<date>_<rev>_s3_upload_protocol.py`
- Modify: `app/config.py`, `app/utils/multipart_config.py`, `app/storage/__init__.py`, `app/cli/__main__.py`, `app/services/multipart.py`, `app/models/core_models.py` (upload session protocol check), `pyproject.toml` and `uv.lock` (`boto3`), `.github/workflows/tests.yml` (new `storage-s3` job)

**Interfaces:**
- Settings in `StorageSettings` (no rename of existing variables):
  - `backend: Literal["gcs", "s3"] = "gcs"` from `STORAGE_BACKEND`
  - `s3_endpoint_url: str | None` (`S3_ENDPOINT_URL`, the address the API uses), `s3_public_url: str | None` (`S3_PUBLIC_URL`, the address in presigned URLs; defaults to the endpoint), `s3_region: str = "us-east-1"` (`S3_REGION`)
  - secrets `s3_access_key_id` (`S3_ACCESS_KEY_ID`) and `s3_secret_access_key` (`S3_SECRET_ACCESS_KEY`)
  - the `MULTIPART_*` variables read through the settings instead of `os.environ` (same names, same defaults)
  - `problems()`: with `backend == "s3"` and storage in use, require `UPLOAD_BUCKET`, `S3_ENDPOINT_URL`, both S3 secrets, and not `GCS_SA`; with `"gcs"`, today's rules. `MULTIPART_UPLOAD_SIGNER` and `MULTIPART_UPLOAD_PROJECT` are required only for `"gcs"`.
- `S3Store(settings: StorageSettings)` implements `ObjectStore`, `retires_by_lifecycle = False`:
  - `sign_part` presigns `upload_part` with `ChecksumCRC32C=crc32c` on the public client and returns headers `{"x-amz-checksum-crc32c": crc32c, "Content-Length": str(size)}`
  - `finish_upload` sends each part's `ChecksumCRC32C` from `crc32c_by_part`
  - `head` returns `ObjectInfo(size, None, version)` with `version` the `VersionId` if present, else the ETag without quotes
  - `signed_download` presigns `get_object` with `ResponseContentDisposition` and `ResponseContentType`, ignoring `version`
- `crc32c_combine(crc_a: int, crc_b: int, length_b: int) -> int`: zlib's `crc32_combine` (GF(2) matrix method) with the Castagnoli polynomial `0x82F63B78`; the multipart service computes the whole-object CRC32C from the authorized part CRCs when `ObjectInfo.crc32c` is `None` and compares it with the completion claim.
- Upload sessions created on S3 use `protocol = "s3_v4"`; the migration widens the check constraint to `('gcs_xml_v1', 's3_v4')`. The public `upload_protocol` stays `"xml-multipart-signed-v1"`.
- `python -m app.cli storage check [--size-mib N] [--keep]`: against the configured store and buckets, runs and reports each step: start a 2-part upload (one 5 MiB part, one 1 KiB part) through presigned part URLs with the returned headers; re-upload part 1; a part with a wrong checksum must be rejected; finish; `head`; presigned download with `attachment` and filename `check file.bin` (bytes and `Content-Disposition` match); `put_bytes` and `get_text` on `RECORD_BUCKET` if set; delete. Exit 0 when every step passes, 1 otherwise. With `--public`, it also checks that `S3_PUBLIC_URL` answers.

- [ ] **Step 1:** `tests/test_crc32c.py`: for random byte strings `a` and `b` (including empty `b`), `crc32c_combine(crc(a), crc(b), len(b)) == crc(a + b)` using `google_crc32c`; run, watch it fail; implement; pass.
- [ ] **Step 2:** Settings tests in `tests/test_config.py`: `STORAGE_BACKEND=s3` with storage in use and no endpoint reports `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`; `GCS_SA` is not required for `s3`; `MULTIPART_UPLOAD_ENABLED=maybe` is still refused; the production and staging ConfigMaps still pass (`tests/test_k8s_pinned_settings.py`, `tests/test_settings_check.py`). Implement; pass.
- [ ] **Step 3:** `tests/test_s3_store.py` with `botocore.stub.Stubber`: presigned part URL signs `host;x-amz-checksum-crc32c` and the returned headers carry the checksum; `finish_upload` sends `ChecksumCRC32C` per part; `head` returns the ETag without quotes as `version`; presigned GET carries `response-content-disposition`; no request uses `aws-chunked` encoding. Implement `S3Store`; pass.
- [ ] **Step 4:** `get_store()` returns `S3Store` for `s3`; the multipart service uses `crc32c_combine` when `ObjectInfo.crc32c` is `None`; migration for `s3_v4`; the lifecycle tests run once with `MemoryStorage` reporting `crc32c=None` to cover the combine path.
- [ ] **Step 5:** `tests/test_s3_store_integration.py` (skipped without `S3_TEST_ENDPOINT`, `S3_TEST_ACCESS_KEY`, `S3_TEST_SECRET_KEY`, `S3_TEST_BUCKET`): the `storage check` sequence through the Python API, plus Review Focus 3 and 4 (a part uploaded twice; a file exactly 2 parts long; a 1-byte file).
- [ ] **Step 6:** CI job `storage-s3`: `docker run -d` SeaweedFS 4.51 (`mini -dir=/data`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET=crucible-files,crucible-records`) and Versity S3 Gateway (`versity/versitygw` pinned, `ROOT_ACCESS_KEY`, `ROOT_SECRET_KEY`, posix backend, bucket created with `aws s3api create-bucket` or boto3), then the integration tests and `python -m app.cli storage check` against each.
- [ ] **Step 7:** Full suite; CHANGELOG (operator-facing: `STORAGE_BACKEND` and the `S3_*` settings, `storage check`).

### Task 3: Cleanup job

**Files:**
- Create: `app/services/storage_cleanup.py`, `tests/test_storage_cleanup.py`, `migrations/versions/<date>_<rev>_storage_retirement.py`
- Modify: `app/models/core_models.py` (`StorageRetirement`), `app/services/multipart.py` (retire through the table when `retires_by_lifecycle` is false), `app/cli/storage.py`

**Interfaces:**
- Table `storageretirement`: `id`, `bucket`, `key`, `version` (nullable), `retire_after` (timestamptz), `deleted_at` (nullable), unique on `(bucket, key)`.
- `run_cleanup(engine, store, now: datetime, dry_run: bool) -> CleanupReport` where `CleanupReport(aborted_uploads: int, deleted_objects: int, failures: list[str])`:
  - aborts the provider uploads of sessions in an open state whose `expires_at` has passed, and marks them cleaned (same states and markers as today's `cleanup`);
  - deletes objects whose `retire_after` has passed and are referenced by no file record (same eligibility rule as `retire_if_eligible`), sets `deleted_at`.
- Grace period: 7 days, as today's GCS lifecycle rule (`STORAGE_RETIRE_AFTER_DAYS`, default 7).
- `python -m app.cli storage cleanup [--dry-run]` prints the report and exits 1 when anything failed.

- [ ] **Step 1:** Tests: an expired open session is aborted once and marked cleaned (a second run does nothing); an unexpired one is left alone; a retired object past `retire_after` and unreferenced is deleted; one still referenced by a file record is kept; `--dry-run` changes nothing; a store failure on one object is reported and the others still run (Review Focus 5). Run; fail.
- [ ] **Step 2:** Migration, model, `run_cleanup`, CLI; the multipart replace on `S3Store` inserts a `storageretirement` row instead of calling `retire`. Pass; full suite; CHANGELOG.

### Task 4: Empty files on the signed upload

Clients refuse 0-byte files on the signed path today, which keeps the legacy routes alive for them.

**Files:** `app/services/multipart.py`, `app/models/multipart.py`, `tests/test_multipart_lifecycle.py`, `docs/multipart_upload_api.md`

**Interfaces:** `initiate` with `size == 0` returns `part_size: 0` and `part_count: 0` without starting a provider upload; `complete` with `parts: []` and the empty CRC32C `"AAAAAA=="` writes the empty object with `store.put_bytes` and publishes the file as any other completion (same response). Any other completion of a 0-part session is refused with 422 `invalid_parts`.

- [ ] **Step 1:** Tests: initiate size 0, complete with no parts, the file exists with size 0 and the `MemoryStorage` object is empty; a wrong CRC is refused; signing parts for a 0-part session is refused. Fail; implement; pass.
- [ ] **Step 2:** Tell the nano-crucible and crucible-hub sessions the contract, so their clients can drop the legacy fallback for empty files.

### Task 5: Legacy upload routes on S3, and deprecation

**Files:** `app/api/routes/gcs_storage.py`, `tests/test_gcs_multipart_endpoint.py`, `tests/test_storage_download_routes.py`, `docs/remote_file_records.md`, `CHANGELOG.md`

- [x] **Step 1 (done in #386):** with `STORAGE_BACKEND=s3`, `POST /datasets/{dataset_mfid}/upload/initiate`, `/upload/initiate/multipart`, and `/upload/complete` answer 501 `{"detail": {"code": "legacy_upload_unavailable", ...}}`, not 503.
- [ ] **Step 2:** With `gcs` the three routes carry a `Deprecation` header and OpenAPI marks them deprecated; document in `docs/remote_file_records.md` that `storage_backend = "gcs"` means "stored by the API" whatever the store; CHANGELOG (clients: the legacy routes are deprecated; self-hosted installations need nano-crucible 3.3.0 or later).
- [ ] **Step 3:** Measure before setting a retirement date: from production's request logs (read-only), how often the three routes are called and by which clients (nano-crucible sends its version in the User-Agent). Known callers from the local repositories (2026-10-29): crucible-hub uses only the signed routes; nano-crucible 3.3.0 uses them by default and falls back to the token route only when the server has multipart switched off, 3.2.1 and earlier only the token routes; the crucible-ingestion environment carries an older nano-crucible that calls the token routes; the upload UIs call neither directly and depend on the nano-crucible version they pin.

### Before the Foundry switch (decided 2026-10-29)

The Foundry moves to `S3Store` only after all of these:

1. **Backfill file locations, then remove the lookup.** Some older records have no `storage_path`; today `_determine_storage_path` lists the dataset's prefix in Google on first use and fills it in, which only works on `gcs` (on `s3` such records answer 404 since #386). Run a one-off, reviewed, dry-run-first backfill that sets every such record's `storage_path` (reads storage, writes the database, needs the user's authorization), then remove `_determine_storage_path`, `_ensure_storage_path`, and their four callers (single and dataset download links, ingest, replace).
2. **Retire the token-based upload routes** (Task 5, once Step 3 shows no remaining callers). They are the only way an object is overwritten in place, and `S3Store` does not pin downloads to an object version (GCS through the S3 API may not support it), so the routes go before the switch, not after.
3. **The `Authorization` header on downloads** (Task 7): released clients send their API key along with the signed download URL. Through the bundle's Caddy it is stripped; on the Foundry the URL points at Google directly, so whether Google's S3 interface accepts a presigned GET that also carries `Authorization: Bearer` must be tested. If it refuses, the switch waits for a nano-crucible release that does not send the key to storage URLs, and for clients to move to it.

### Task 6: SeaweedFS in the Compose bundle

Status 2026-10-29: built and tested, PR after #386. Differences from the steps below: the hourly `storage-cleanup` service moves to Task 3 (it needs the cleanup command); added `CRUCIBLE_FILES_DIR` (a folder of the lab's choice instead of the Docker volume) and a `files-backup` service (rclone through the S3 interface, incremental, plain files, store running) instead of stopping the store for a volume copy; the SeaweedFS health check waits for the buckets (through the filer on port 8888). The Caddy hop was measured: a 1 GiB download runs at about 2.5 GB/s through Caddy against 4.4 GB/s straight from SeaweedFS on one machine (plain HTTP), far above a 10 Gb link, so Caddy stays; a lab that needs more bandwidth can give the store its own address (README.md) or use its institution's store, which clients reach directly.

**Files:** `deploy/compose/compose.yaml`, `deploy/compose/Caddyfile`, `deploy/compose/.env.example`, `deploy/compose/README.md`, `.github/workflows/tests.yml` (`compose-smoke`), `scripts/selfhost_smoke.py`

- [ ] **Step 1 (spike, before the rest):** In a scratch copy of the bundle, put SeaweedFS behind Caddy with `handle /crucible-files/*` (no `handle_path`, no Host rewrite) and run the Task 2 `storage check` with `S3_PUBLIC_URL` set to the site address: a presigned part upload and a presigned download must pass through Caddy, including with `Authorization: Bearer x` added to the download. If it fails, fall back to the dedicated subdomain option from the research and record why.
- [ ] **Step 2:** `seaweedfs` service: `chrislusf/seaweedfs:4.51`, `mini -dir=/data`, credentials from the secret files `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` (an entrypoint wrapper exports them as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`), `S3_BUCKET=crucible-files,crucible-records`, volume `seaweedfs-data`, health check on the S3 port, `cap_drop: [ALL]` and `no-new-privileges` (relax only what SeaweedFS needs), profile `bundled-storage` enabled by `COMPOSE_PROFILES=bundled-storage` in `.env.example`. A new internal network `storage` joined by `seaweedfs`, `api`, and `proxy` only; the Hub cannot reach it.
- [ ] **Step 3:** Caddy: `handle /{$CRUCIBLE_STORAGE_BUCKET}/*` with `request_header -Authorization`, `request_header -Cookie`, response headers `Content-Security-Policy: sandbox` and `X-Content-Type-Options: nosniff`, `reverse_proxy seaweedfs:8333`; the API sets `STORAGE_BACKEND=s3`, `S3_ENDPOINT_URL=http://seaweedfs:8333`, `S3_PUBLIC_URL` = the site address, `UPLOAD_BUCKET`, `RECORD_BUCKET`, `MULTIPART_UPLOAD_ENABLED=true`, `MULTIPART_UPLOAD_BUCKET`. A `storage-cleanup` service runs `python -m app.cli storage cleanup` hourly (shell loop), on the `backend` and `storage` networks.
- [ ] **Step 4:** Smoke test: after the record steps, upload a file through `scripts/multipart_upload_client.py` against the public address, download it through `download_link` with an `Authorization` header (Review Focus 2), replace it, run `storage check` in the `api` container. CI `compose-smoke` runs it; the upgrade path from the previous bundle (no storage) is tested as in Task 9 of Phase 1.
- [ ] **Step 5:** Install guide: storage section (what is stored where, one address for everything with Review Focus 1, using an institution's S3 store instead by leaving the profile out and setting the `S3_*` settings, the subdomain alternative), secrets (two more files), backups of `seaweedfs-data` (stop the service or snapshot the volume; restore order with the database), the nano-crucible 3.3.0 minimum. CHANGELOG.

### Task 7: Proving GCS through its S3 API (gate for the Foundry)

No code unless the check finds a gap.

- [ ] **Step 1 (user):** Confirm in the console that the LBL organization does not enforce `iam.disableServiceAccountKeyCreation` (or its managed form) or `storage.restrictAuthTypes`; create a disposable bucket, a dedicated service account with Storage Object Admin on that bucket only, and an HMAC key for it.
- [ ] **Step 2:** Run `python -m app.cli storage check` with `STORAGE_BACKEND=s3`, `S3_ENDPOINT_URL=https://storage.googleapis.com`, `S3_REGION=auto`, the bucket, and the HMAC key, from a workstation; record each step's result in `docs/self_hosting_phase2_research.md` (part checksum enforcement including the changed and missing header cases, completion, head, signed download with filename, delete). Also download a presigned URL with an `Authorization: Bearer x` header added, as released clients do (see "Before the Foundry switch", item 3).
- [ ] **Step 3:** If every step passes, propose the staging switch to the user and the deploy session (settings and secrets in mf-crucible-deploy, staging first, crucible-tests run, then production); after production runs on `S3Store`, remove `GoogleStore`, `google-cloud-storage`, and the legacy routes in a later release. If a step fails, `GoogleStore` stays, and the result is recorded with the reason.

## Open questions

1. SHA-256: the server takes the client's hash on trust today (`sha256_verified_at` is never set). Either the cleanup job (or a separate one) reads each new object once and sets `sha256_verified_at`, or the field is documented as the client's claim. Reading every object costs one full read of each upload.
2. Testing on staging after the cut-over: patch releases or release-candidate tags (see "Process change after the Foundry cut-over").
3. `docs/multipart_upload_api.md` is out of date (old staging bucket name, a default `UPLOAD_BUCKET` that no longer exists, schema head); correct it in Task 4, which edits it anyway.

## Done when

- A fresh Compose install uploads, replaces, downloads, and deletes-by-replacement files with nano-crucible 3.3.0 and the Hub, and CI proves it on every push.
- `storage check` passes against SeaweedFS and Versity in CI, and its result against GCS is recorded.
- The Foundry runs unchanged on `GoogleStore`, verified on staging after Tasks 1 to 5.
