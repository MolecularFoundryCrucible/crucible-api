# Secure parallel upload migration

## Status

Deferred. This document records the agreed direction for future work. The current API and client behavior remain unchanged.

## Current behavior and risk

`POST /datasets/{dataset_mfid}/upload/initiate/multipart` returns a short-lived OAuth token derived from the API storage service account. The returned `object_prefix` is informational and does not restrict the token. A client can use the token anywhere permitted by the service account instead of limiting access to the requested dataset or object.

Nano currently passes this token to `google-cloud-storage` and calls `transfer_manager.upload_chunks_concurrently()`. That implementation performs a parallel composite upload using temporary objects. Parallel upload performance is required for very large datasets, so replacing it immediately with sequential resumable upload is not acceptable.

## Agreed direction

Retain the existing public route structure:

```http
POST /datasets/{dataset_mfid}/upload/initiate/multipart
POST /datasets/{dataset_mfid}/upload/complete
```

Replace the internal parallel composite upload protocol with Google Cloud Storage XML multipart upload. XML multipart supports concurrent part uploads into one final object and supports up to 10,000 parts.

The API should issue a short-lived OAuth token with a Credential Access Boundary restricted to the exact target object. Its permissions should be limited to those required for multipart part creation. The client must never receive the unrestricted service-account token.

Google references:

- [XML API multipart uploads](https://docs.cloud.google.com/storage/docs/multipart-uploads)
- [Credential Access Boundaries for Cloud Storage](https://docs.cloud.google.com/iam/docs/downscoping-short-lived-credentials)
- [Cloud Storage multipart permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-permissions)

## Future API behavior

### Initiation

`POST /datasets/{dataset_mfid}/upload/initiate/multipart` should:

1. Validate the declared size and filename.
2. Require edit permission on the dataset.
3. Apply the existing SHA-256 deduplication check.
4. Select and reserve the exact destination object.
5. Initiate an XML multipart upload through the API's storage credential.
6. Persist the provider upload ID in the Crucible upload session.
7. Mint a short-lived token downscoped to the exact destination object.
8. Return the Crucible upload ID, provider upload ID, exact object name, part-size recommendation, token, expiry, bucket, and protocol identifier.

The response should identify the protocol explicitly, for example `upload_protocol: "xml-multipart-v1"`. The current `object_prefix` should be replaced by an exact `object_name` for this protocol.

### Part upload

Nano should split the file into numbered parts and upload them concurrently with XML API `PUT` requests. Each request should contain the provider upload ID, part number, downscoped bearer token, and CRC32C checksum. Nano should retry individual failed parts without restarting the complete upload.

### Completion

`POST /datasets/{dataset_mfid}/upload/complete` should retain its existing public path. For an XML multipart session it should:

1. Verify the Crucible upload-session owner, expiry, dataset association, and current edit permission.
2. List the uploaded parts through the API's storage credential.
3. Complete the XML multipart upload server-side.
4. Verify the final object size and checksum information.
5. Create the associated-file record and remove the upload session in a consistent completion flow.

The client should not need to submit every part ETag if the API can reliably list the parts before completion. This must be confirmed in the staging implementation.

### Expiry and cleanup

GCS multipart uploads do not expire automatically. Expired or abandoned Crucible sessions must therefore be aborted explicitly. Bucket lifecycle configuration should also remove abandoned multipart uploads as a safety net.

## Data model changes

Use an additive Alembic migration so deployment remains compatible with active resumable uploads. `UploadSession` will need at least:

- `upload_protocol`, distinguishing resumable and XML multipart sessions.
- `provider_upload_id`, storing the GCS multipart upload ID.

Do not overload the Crucible `upload_id`, which identifies and authorizes the application-level session.

## Deployment recipe

1. Enable and verify the Google Cloud IAM and Security Token Service APIs required for Credential Access Boundaries.
2. Add the nullable upload-session columns and deploy the compatible API schema.
3. Add `xml-multipart-v1` support to the existing initiation and completion routes while retaining the legacy behavior temporarily.
4. Update Nano to request `upload_protocol="xml-multipart-v1"` and use parallel XML part uploads.
5. Test large uploads, retries, checksum failures, session expiry, abort cleanup, deduplication, and concurrent completion in staging.
6. Deploy the updated client.
7. Make XML multipart the default and reject legacy composite initiation.
8. Remove unrestricted upload-token minting and the legacy response fields after the compatibility window.

The overlap window must be short because the legacy protocol continues to expose the service account's effective storage permissions.

## Required validation before implementation

- Confirm that the intended Credential Access Boundary condition applies to every XML multipart request for the exact object.
- Use the narrowest role that includes `storage.objects.create` and `storage.multipartUploads.create`; create a custom role if a predefined role grants unnecessary read, list, update, or delete access.
- Confirm whether completion and abort operations remain server-only and exclude those permissions from the client token.
- Confirm behavior when the destination object already exists because XML multipart requests do not support normal object preconditions.
- Establish minimum and maximum part sizes and choose a default that remains below the 10,000-part limit for `MAX_UPLOAD_BYTES`.
- Confirm how final CRC32C and client-computed SHA-256 values are recorded and verified.
- Ensure logs never contain access tokens, provider upload IDs, or authorization headers.
