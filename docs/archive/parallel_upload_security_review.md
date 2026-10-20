# Parallel upload security review

## Scope and conclusion

Reviewed the current GCS parallel upload initiation and completion routes, their request and response models, the upload-session model, and the existing migration plan on 2026-09-28. This is a code and documentation review. Production IAM bindings, bucket settings, and the Nano client implementation were not available to verify. The current parallel upload route should not be offered to production clients because it returns a reusable service-account token without an object or dataset boundary.

## Critical

### PU-1: Client receives the storage service account's effective read/write access

- **Status (2026-10-13):** Mitigated. `_mint_upload_token` wraps the service-account token in a Credential Access Boundary that allows `roles/storage.objectAdmin` only on objects whose name starts with `{dataset_mfid}/` in the upload bucket. A live probe against a disposable bucket confirmed that `transfer_manager.upload_chunks_concurrently`, overwrite, and `blob.reload()` work inside the prefix, while reads, writes, deletes, and listings elsewhere (other dataset prefixes, look-alike sibling prefixes, `uploads/` multipart objects) return 403. Contributors can still overwrite files of the same dataset (PU-2), which is kept intentionally for re-uploads. `LEGACY_UPLOAD_TOKEN_DOWNSCOPE=false` restores the broad token for emergency rollback only.
- **Location:** `app/api/routes/gcs_storage.py:106-113` (`_mint_upload_token`), `app/api/routes/gcs_storage.py:230-264` (`initiate_multipart_upload`), and `app/models/uploads.py:32-43`.
- **Evidence:** `_mint_upload_token` refreshes service-account credentials with `devstorage.read_write`; the route returns that token as `access_token`. `object_prefix` is a response field only. It is not included in a Credential Access Boundary or an IAM condition.
- **Impact:** Any caller with `CREATE` permission on one dataset can use the returned bearer token directly against any GCS object allowed by the service account's IAM grants until token expiry. Depending on those grants, this can expose or alter data outside that dataset. Application upload-session expiry, permission changes, and completion checks do not revoke the token.
- **Fix:** Stop issuing the unrestricted token to clients. Preserve parallel transfer with a protocol that supports credentials downscoped to the exact destination object, or keep all GCS authority server-side. Use a narrowly permissioned role and verify the object condition against every required request in staging.
- **Interim mitigation:** Disable this initiation route for production clients until a safe replacement is deployed. Verify the service account's effective IAM grants and storage audit logs; the repository does not establish their scope.
- **False-positive condition:** A separate deployment control could prevent clients from using this token against other objects, but no such control is visible here. An object prefix in the JSON response is not one.

## High

### PU-2: A create-only user can overwrite a prior object's bytes

- **Location:** `app/api/routes/gcs_storage.py:134-155` (`_validate_and_prepare_upload`), `app/api/routes/gcs_storage.py:250-264` (`initiate_multipart_upload`), and `app/models/core_models.py:41-51` (`AssociatedFile`).
- **Evidence:** The route checks `Permission.CREATE`, derives the destination deterministically as `{dataset_mfid}/{filename}`, and does not reserve a unique object name or use an object-generation precondition. Deduplication checks a supplied SHA-256 value, not filename or existing storage path. `AssociatedFile` has no uniqueness constraint on dataset and filename or storage path.
- **Impact:** Two uploads with the same filename can target the same live GCS object. A contributor who can add files but lacks `EDIT` or `DELETE` permission can replace bytes used by an existing file record if the service account permits overwrite. Concurrent uploads can also create records whose storage path now serves the other upload's bytes.
- **Fix:** Allocate a unique immutable storage object per upload or reserve a destination transactionally and enforce an appropriate generation precondition. Bind completion to that exact object generation. Explicitly define whether replacing an existing file requires a stronger permission.
- **False-positive condition:** An external bucket policy might block overwrites, but it is not represented in this repository and must be verified.

### PU-3: Completion does not prove the object belongs to the session or that its digest matches

- **Location:** `app/api/routes/gcs_storage.py:267-325` (`complete_resumable_upload`) and `app/models/uploads.py:26-29`.
- **Evidence:** Completion reloads the blob named by the session and compares only its size with the declared size. It then records `body.sha256_hash`, which the client may choose at completion. The upload session stores neither a provider upload ID nor the completed object's generation or verified checksum.
- **Impact:** If an object already exists at that path with the expected size, the caller can register it without completing this session's upload. A concurrent overwrite can cause a valid session to register different bytes under a false SHA-256 value. This also makes hash-based deduplication unreliable.
- **Fix:** Tie completion to a provider upload and final object generation. Verify the final size and a server-verifiable checksum, then record only a trusted digest. Make session completion atomic so two requests cannot register the same session twice.
- **False-positive condition:** A trusted client might normally send a correct hash, but the endpoint accepts untrusted client input and has no server-side enforcement.

## Medium

### PU-4: Abandoned uploads and stale sessions lack an operational cleanup path

- **Location:** `app/api/routes/gcs_storage.py:158-179` (`_create_upload_session`), `app/api/routes/gcs_storage.py:281-295` (`complete_resumable_upload`), and `migrations/data/20260507_create_upload_sessions.sql:1-16`.
- **Evidence:** Sessions have a seven-day expiry, but the route deletes an expired row only when the client calls completion. The migration file contains a commented cleanup SQL statement, and no scheduled cleanup or abort endpoint appears in the reviewed code. Correction from the subsequent proposal review: Google's `upload_chunks_concurrently()` uses XML multipart, not parallel composite uploads. The installed library confirms this; Nano's precise version still needs checking.
- **Impact:** Abandoned sessions remain in the database, and failed or abandoned parallel transfers can leave billable multipart parts. Deleting a session row alone would not abort their provider upload.
- **Fix:** Add an explicit abort flow and a scheduled cleanup process that reconciles provider uploads and session rows. Configure bucket lifecycle cleanup as a fallback, and test interrupted large uploads.
- **False-positive condition:** A separately managed cleanup job or bucket lifecycle rule may exist outside the repository; confirm it in the deployed environment.

### PU-5: File records can point downloads at arbitrary objects

- **Status (2026-10-13):** Fixed. Found during the storage review: `PATCH /files/{file_id}` accepted any `storage_path`, and download and ingestion signed or forwarded it with the project-owner `GCS_SA` identity. Downloads and ingestion now enforce the location rule in `app/services/storage_downloads.py`, and `PATCH` no longer changes where a GCS file's bytes live. See [storage location confinement](../storage_download_design.md).

## Recommended release path

The updated [secure parallel upload migration plan](secure_parallel_upload_migration.md) proposes separate routes, signed part URLs, and gradual client migration. The previous exact-object token proposal could not enforce server-only completion: uploading parts and completing a provider upload require the same core permissions in Google's [XML IAM table](https://docs.cloud.google.com/storage/docs/access-control/iam-xml). The proposal now compares both approaches, specifies storage isolation during coexistence, and corrects the protocol description using the [Python transfer-manager reference](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.transfer_manager). Provider authorization, overwrite behavior, integrity verification, and deployment settings still need staging validation.

Review method: static inspection only; no production credentials, bucket changes, migrations, or writes were used. The existing endpoint tests mock token minting and assert that the token is returned, so they do not exercise IAM boundaries, object generation, checksum verification, or concurrent completion.
