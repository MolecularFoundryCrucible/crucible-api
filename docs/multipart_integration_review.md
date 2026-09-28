# Multipart integration review

Integrated `origin/main` at `8e4d0ef` into `feat/secure-multipart-upload`, including the completed auth workflow and its deployment/readiness changes. This review covers the multipart routes, worker, storage adapter, file integration, generated contract, migration ordering, and interaction with the shared authentication dependency. No shared database migration, deployment, cloud write, or push was performed.

## Findings fixed

| Priority | Finding | Resolution |
| --- | --- | --- |
| High | The application image did not include the multipart worker entry point, although the operator guide instructed deployment from that image. | Added `scripts/multipart_worker.py` to the Docker image while preserving Hub cleanup packaging. |
| Medium | A worker finishing a sweep could overwrite a newly requested completion/cancellation schedule with its normal polling delay. | Worker release compares the persisted schedule with the claimed schedule and keeps intervening requests immediately due. Added a cancellation-race regression test. |
| Medium | A provider upload removed out of band left an application session in `uploading`, with status repeatedly promising reconciliation until expiry. | The sweep now detects a missing primary provider upload, records `provider_upload_missing`, and schedules cleanup so reservations can be released. Added a recovery regression test. |
| Medium | Background completion retained initiation-time administrator elevation even if the completion request explicitly chose normal privilege mode. | Freeze the completion request's effective privilege mode when queuing finalization. A regression test revokes the ACL after normal-mode completion and verifies that the worker rejects publication. |
| Medium | An editable catalog path could block orphan cleanup without a trusted registration. | Cleanup protection now requires a server-recorded storage generation; tests cover both untrusted references and protected registrations. |
| Integration | Auth and multipart migrations otherwise formed separate heads, and the readiness templates still expected the auth-only revision. | Reparented only the unapplied foundation revision after auth head `e92a6b1c730f`; processing head remains `c83a7d014b29`. Updated local readiness templates for the future coordinated release. Applied auth revisions are unchanged. |

## Authentication and compatibility

Multipart uses the existing `authorize_user` dependency. Both a legacy Crucible API key and a Crucible access token issued by the ORCID/OAuth login workflow are accepted as bearer credentials. Upload ownership is attached to the principal, not to an individual token, so refreshed credentials can resume the same upload. Raw ORCID provider tokens and Hub refresh tokens are not API access credentials.

New integration tests exercise initiation, status, part authorization, completion, and publication using each real credential path. The OAuth case refreshes its token during the upload and verifies that revocation rejects subsequent status and cancellation requests. Workers continue already accepted durable jobs subject to fresh resource permission checks; logging out does not itself cancel an upload or revoke an already issued GCS part URL.

OpenAPI was regenerated from the combined application rather than hand-merged. Every path and component schema present in `origin/main` is preserved. Multipart adds five operations across four paths. The generated checksum is updated.

## Validation

- Final PR review local suite: 1,363 passed, 69 environment-dependent skips; existing deprecation warnings remain.
- After the completion privilege-mode fix, 50 multipart lifecycle and shared privilege-mode tests passed.
- Disposable PostgreSQL: 9 auth and multipart migration/concurrency tests passed, including token exchange/refresh behavior, multipart admission/lease races, and multipart upgrade/downgrade round trips.
- Alembic has one head: `c83a7d014b29`, following `e92a6b1c730f -> b72f6c903a18 -> c83a7d014b29`.
- Pyflakes on the changed worker/test modules and generated-contract comparison passed.

## Remaining release requirements

New multipart initiation remains disabled by default. No live application-to-GCS pilot or large-file benchmark was performed during this review. Permanent bucket isolation, runtime impersonation, browser origins, actual ingestion-consumer support, lifecycle/retention behavior, and worker supervision still require staging validation. Native client rollout remains separate from this API branch.

The readiness configuration changes are source changes only. Coordinate the additive migrations and the API/config rollout in an authorized deployment window: running deployments check an exact schema revision, and the new application queries the added columns even while multipart initiation is disabled. Do not deploy these source templates independently of that migration plan. A full container image build was not part of this local review; packaging was checked in the Dockerfile and existing Hub packaging tests.
