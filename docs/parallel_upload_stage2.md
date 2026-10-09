# Multipart stage 2: database foundation

Implemented on the isolated multipart branch. No staging or production schema changes have been applied by this stage.

The existing `upload_sessions` and `associatedfile` tables receive nullable columns. Null protocol retains legacy behavior for both existing upload styles. New sessions use `gcs_xml_v1`, constrained states, and owner/dataset/protocol-scoped idempotency keys. JSONB holds the future bounded part manifest; application enforcement of manifest limits and state transitions belongs to the route/worker implementation. File generation, CRC32C, and SHA-256 verification timestamps are internal metadata and do not alter public response models. Historical SHA-256 values remain unverified.

Legacy completion rejects non-null protocols after checking ownership, before expiry deletion or storage access. Dataset deletion retains new sessions and marks cleanup intent in the same database transaction. A later worker must consume that intent, coordinate with finalization, and clean provider uploads/objects before pruning records. This foundation does not yet perform that cleanup.

## Configuration

`MULTIPART_UPLOAD_ENABLED` defaults to `false` and accepts only `true` or `false`. Enabling requires explicit `MULTIPART_UPLOAD_BUCKET`, `MULTIPART_UPLOAD_SIGNER`, and `MULTIPART_UPLOAD_PROJECT`. The legacy production bucket is rejected. Configuration is validated when the API imports its application. These settings prepare the new storage interface; no new route or credential client is enabled by this change. Keep the flag false until the route, worker, and deployment acceptance checks are complete. IAM isolation still requires live verification for the selected environment.

## Integration and deployment gates

Integration update: the completed auth changes from `origin/main` at `8e4d0ef` are included. The unapplied multipart foundation revision `b72f6c903a18` now follows the staging head `b7e2d9c4a1f3`; processing revision `c83a7d014b29` follows the foundation. The combined branch has one Alembic head. No applied auth revision was rewritten and no shared migration was run.

Apply the additive migration before deploying code that selects the new columns. Deploy the protocol guard to every instance before any new initiation is enabled. Existing client request/response schemas are unchanged. Once new sessions exist, do not roll back to binaries lacking the guard or remove the added columns; disable new initiation while retaining processing and cleanup support.

Next implement the dedicated storage client, signed-part routes, bounded manifests, current permission checks, leased processing and reconciliation, generation-pinned publication (provider-validated CRC32C before publication; SHA-256 verification is a later background-worker addition), and download/ingestion/delete integration. No new endpoints are exposed in stage 2.

## Local validation

Passed 25 focused tests across `test_multipart_foundation.py`, `test_multipart_migration.py`, `test_gcs_multipart_endpoint.py`, and `test_database_deletion_contract.py`. PostgreSQL 16 in a disposable local container verified upgrade, legacy rows and inserts, uniqueness and state constraints, downgrade, and re-upgrade in a rolled-back temporary schema. SQLite tests verified legacy completion handling, cross-protocol rejection before storage/deletion, idempotency scope, invalid geometry/state rejection, and atomic dataset deletion cleanup intent. Existing legacy initiation and dataset deletion tests passed. The test container was removed afterward.

`alembic heads` reports the single branch head `c83a7d014b29`; `git diff --check` and Pyflakes for the new configuration, migration, and test modules passed. These checks do not establish production readiness or validate the future storage client/worker. Tests used explicit dummy configuration with dotenv loading disabled and the original checkout's Python environment.
