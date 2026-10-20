# XML multipart stage 1 experiment

## Result

Completed 2026-09-28. The signed-part-URL design passed the live GCS experiment and is suitable to take into stage 2. The Python probe passed 38 recorded checks, including cleanup; a separate Firefox probe passed its initiation, browser upload, and abort checks. These checks establish provider behavior with small synthetic payloads, not production readiness of an API that has yet to be implemented.

Evidence:

- [Provider and legacy credential checks](parallel_upload_stage1_results.json)
- [Firefox upload result](parallel_upload_stage1_browser_results.json)
- [Bucket configuration and final inventory](parallel_upload_stage1_inventory.json)
- [Provider probe](../../scripts/gcs_multipart_probe.py)
- [Browser probe](../../scripts/gcs_multipart_browser_probe.py)

No application routes, public API models, database tables, production buckets, or production IAM bindings were changed. Infrastructure created for this experiment is listed below.

## What passed

| Check | Observed result |
| --- | --- |
| Server initiation and keyless V4 URL signing | Successful using a dedicated impersonated service account |
| Parallel part transfer | Two parts, 5 MiB and 1 MiB, uploaded directly to GCS with HTTP 200 |
| Retry and URL renewal | Reuploading the same part and using a newly signed URL both succeeded |
| Altered bucket, object, upload ID, or part number | HTTP 403 `SignatureDoesNotMatch` |
| Altered request method | POST, DELETE, and GET rejected with HTTP 403 `SignatureDoesNotMatch` |
| Altered signed checksum header or content length | HTTP 403 `SignatureDoesNotMatch` |
| Corrupted bytes with the original checksum | HTTP 400 `BadDigest` |
| Expired URL | HTTP 400 `ExpiredToken` |
| Paginated part listing | Both parts recovered with `max-parts=1`, requiring two pages |
| Server completion | Successful; resulting size and CRC32C matched the source |
| Reading the completed generation and computing SHA-256 | Matched the source SHA-256 |
| Reusing a part URL after completion or abort | HTTP 404 `NoSuchUpload` |
| Browser signed content length | Firefox 156 supplied its own Content-Length and successfully uploaded a 1 MiB byte array |
| Browser CORS | PUT succeeded from the allowed localhost origin; ETag and checksum response headers were readable |
| Legacy credential isolation | Listing, reading, creating, overwriting, deleting, and starting an XML multipart upload were all rejected with HTTP 403 `AccessDenied` |
| Final storage inventory | Zero live probe objects and zero unfinished probe uploads in both buckets |

The legacy credential tested was `GCS_SA` from the local `.env`, identifying `cloud-sql@mf-crucible.iam.gserviceaccount.com`. Its direct project grants inspected during setup were Cloud SQL roles. The actual denial tests are stronger evidence than inspecting those direct grants alone, but they establish isolation for this credential and these buckets, not every identity deployed in production.

The alternate bucket also granted the signer object-admin access. This makes the changed-bucket signature test meaningful: the HTTP 403 came from the changed signed request, rather than a missing target bucket or a signer that could never access it.

## Decisions for implementation

1. Use the separate signed-part route family from the [migration proposal](secure_parallel_upload_migration.md). The experiment demonstrates native SDK signing of PUT, object name, provider upload ID, part number, checksum header, and length without exposing an OAuth token to the uploader.
2. Sign the expected Content-Length. Python requests and Firefox's byte-array upload both worked. Browser callers must let the browser set this forbidden request header; return expected byte counts separately from headers the browser must explicitly send. Verify other supported browsers and real file slices during client integration.
3. Handle provider errors by status and error code. Expiry returned `400 ExpiredToken`, so clients must not assume every authorization-related rejection is HTTP 403. Refresh expired capabilities through the application API and never fall back to a broad token.
4. Keep CRC32C transport verification and SHA-256 trust separate. Recommend trusted background SHA-256 verification before publishing a file as verified or using its digest for trusted deduplication. The generation-pinned read worked; the worker and database publication state belong to later phases. The small-file read timing is not a basis for predicting 500 GiB cost or latency.
5. Use a separate storage identity and isolated destination bucket for coexistence. The experiment proves that this arrangement can deny the tested legacy credential while allowing the new signer. Verify the deployed legacy identities and effective grants before production rollout.
6. Preserve existing client routes and schemas. This experiment made no client-facing changes.

## Client facts confirmed locally

The installed `nano-crucible` is version 2.1.2 and calls `transfer_manager.upload_chunks_concurrently()` from `crucible/resources/files.py`. Installed `google-cloud-storage` is 3.4.1 and uses XML multipart internally. Nano currently receives an OAuth token, constructs the destination from `object_prefix` and the basename, invokes the native transfer manager, reloads the completed blob to check CRC32C, and calls the existing API completion route. It uses a 64 MiB default part size.

The new client flow must replace that upload orchestration with signed part PUTs and application status/completion requests. It must also stop calling `blob.reload()` with a broad token; the server will verify final metadata. These observations describe the locally installed client, not the full deployed client fleet.

## Experiment infrastructure

| Resource | Value |
| --- | --- |
| Project | `mf-crucible` |
| Region | `us-central1` |
| Upload bucket | `mf-crucible-mpu-stage1-20260928-a` |
| Alternate signature-test bucket | `mf-crucible-mpu-stage1-20260928-b` |
| Keyless signer | `mpu-stage1-20260928@mf-crucible.iam.gserviceaccount.com` |
| Signer storage grant | `roles/storage.objectAdmin` on the two test buckets only |
| Impersonation grant | `roles/iam.serviceAccountTokenCreator` for `roncoroni@lbl.gov` on the test signer only |
| Public access | Prevention enforced; uniform bucket-level access enabled |
| Incomplete-upload fallback | Abort after one day for the `mpu-stage1/` prefix |
| Browser origin | `http://127.0.0.1:8765`, PUT only, on bucket A |

The buckets and signer remain available for subsequent validation. Test files were deleted and all known provider uploads aborted; the final inventory confirms no live objects or unfinished uploads. Default seven-day soft-delete retention remains enabled, so deleted test object versions may persist temporarily. No service-account private key was created. The signer permissions are suitable for the disposable provider experiment; production IAM still needs a scoped role review.

The first provider attempt encountered an authentication refresh failure immediately after provisioning. A subsequent credential/impersonation check succeeded, and the complete run then succeeded. Allow for IAM propagation in deployment automation rather than treating the first successful provisioning command as proof that all grants are immediately usable.

## Repeating the experiment

The scripts do not import the API or connect to its database. Both default to plan mode. `--run` explicitly enables synthetic writes, and resource-name guards reject buckets and signer names outside the experiment naming convention. ADC must be allowed to impersonate the test signer; the CLI account and ADC are separate credential sources. The main probe also needs the local legacy `GCS_SA` credential but does not print its key or token.

```bash
.venv/bin/python scripts/gcs_multipart_probe.py \
  --bucket mf-crucible-mpu-stage1-20260928-a \
  --other-bucket mf-crucible-mpu-stage1-20260928-b \
  --signer mpu-stage1-20260928@mf-crucible.iam.gserviceaccount.com \
  --run --output /tmp/multipart-provider-results.json

.venv/bin/python scripts/gcs_multipart_browser_probe.py \
  --bucket mf-crucible-mpu-stage1-20260928-a \
  --signer mpu-stage1-20260928@mf-crucible.iam.gserviceaccount.com \
  --run --output /tmp/multipart-browser-results.json
```

While the browser probe runs, open `http://127.0.0.1:8765/` in a real browser. The server receives the result and aborts its provider upload. It holds signed URLs in memory and prints only the localhost page URL and a sanitized result. Do not collect browser network traces containing signed URLs.

The provider probe cleans up objects and uploads from its own random prefix in `finally`. The lifecycle fallback covers unfinished uploads after an abrupt process termination. A hard crash after GCS completion can still leave an unpublished object; the production reconciler remains necessary. Do not run broad recursive bucket deletion as part of these probes.

## Limits and remaining work

- The test used 6 MiB for the parallel transfer and 1 MiB for the browser transfer. Large-file throughput, memory use, rate limits, and SHA-256 verification cost still need measurement.
- Pagination was exercised with a one-part page size; an 8,000-part upload was not performed.
- Firefox was tested. Chromium, WebKit, mobile browsers, and browser file slices remain client-integration checks.
- The one-day lifecycle fallback was configured and inspected, not observed firing over a full day.
- Session authorization, database idempotency, durable workers, crash recovery, quota enforcement, filename reservation, and production download/ingestion integration are not implemented by this probe.
- CORS was tested for one allowed origin. The production allowlist and rejected-origin behavior need integration coverage.
- Infrastructure isolation applies to the new test buckets and the tested local legacy credential. Stage 2 must carry it into the intended deployment configuration and verify all relevant credentials.

Stage 2 can now implement the additive database/storage foundation using the validated signed-URL approach. Production deployment remains a later phase.
