# Documentation index

The deployed OpenAPI document is authoritative for the HTTP contract. This index lists current guidance, audits, and open follow-ups. Finished work is in git history; [`archive/`](archive/README.md) keeps the few dated records still cited.

## Current API and client guidance

- [GET query contract](get_query_contract.md): collection filters, sorting, pagination, anchors, facets, and compatibility behavior.
- [Permission system API changes](permission_system_api_changes.md): role, access, ownership, membership, and authorization contracts.
- [Resource identifier routes](resource_identifier_routes.md): canonical MFIDs, human-readable lookups, and compatibility aliases.
- [Database deletion contract](database_deletion_contract.md): deletion requests, direct force-delete, and how records pending deletion behave.
- [Dataset instrument assignment](dataset_instrument_assignment.md): creation, PATCH compatibility, and atomic reassignment behavior.
- [Instrument access](instrument_access.md): maintainers on the instrument record, the service-account operator group, and dataset access.
- [Join requests](join_requests_design.md): requesting, reviewing, listing, and faceting project join requests.
- [Resource timestamp policy](resource_timestamp_policy.md): database-owned creation and modification times and the separate producer timestamp field.
- [Resource graph API](resource_graph_api.md) and [graph route migration](graph_routes_migration.md): graph security behavior and deferred route consolidation.
- [Remote file records](remote_file_records.md): catalog records for files the API does not store.
- [Signed multipart uploads](multipart_upload_api.md): the multipart upload contract and operator guide.
- [Storage location confinement](storage_download_design.md): the location rule for downloads and ingestion, download headers, and the multipart `_files` layout.
- [File replacement](file_replace_design.md): replacing a stored file's content, ETags, and retirement of the previous object.
- [Explicit privilege mode](explicit_privilege_mode_design.md): the `Crucible-Privilege-Mode` header and elevated requests.
- [Route package conventions](../app/api/routes/readme.md): route module and shared dependency guidance.

## Authentication and operations

- [Installing with Docker Compose](../deploy/compose/README.md): running Crucible on one machine, secrets, the first administrator, backups, upgrades, and reverse proxies.
- [Hub authentication overview](hub_auth_overview.md): sign-in, the API tables, refresh, logout, and cleanup.
- [Hub login integration](hub_login_design.md): the implemented contract and integration requirements.
- [Hub authentication operations](hub_auth_operations.md): secrets, logging, the on/off switch, cleanup, and tests.
- [API-key retrieval and rotation](api_key_lifecycle_followup.md): key deprecations and the authentication cache window.
- [Database migration guidance](../migrations/README.md): schema and data migration safeguards.

## Audits

- [Routes audit, October 2026](routes_audit_2026-10.md): overlapping, untested, and inconsistent routes, and the 4.0 list.
- [Structure audit, October 2026](structure_audit_2026-10.md): files to split or move, configuration, migrations, and tests.

## Open follow-ups

- [Sharing and access data](sharing_collections_followup.md): the collections idea, production access repairs, and records without any grant.
- [Legacy slug columns](legacy_slug_columns_followup.md): dropping the columns the API no longer reads.
- [File name uniqueness](file_name_uniqueness_followup.md): existing duplicate file records.
- [Stored objects left behind by file deletion](file_object_cleanup_followup.md)
- [Message queue publishing](message_queue_publishing_followup.md): ingestion and postprocessing publishing problems.
- [Metadata search performance](metadata_search_followup.md)
- [S3-compatible storage backends](multipart_storage_backends.md)
- [User representation and search](user_representation_and_search_followup.md): one user schema with null fields, the `/users/search` envelope, and a possible cross-type search.
- [Printer registry](printer_registry_followup.md): a database-backed printer list with search, and printing as an optional integration.
- [`/project_graph` performance](project_graph_performance_followup.md)
- [Per-worker Prometheus counters](prometheus_multiprocess_followup.md) and [public `/metrics` endpoint](public_metrics_endpoint_followup.md)
- [Error responses](error_responses_followup.md): moving repeated route error documentation into `app/api/errors.py`.
- [Database engine](database_engine_followup.md): moving routes to a FastAPI session dependency.
- [Secret key separation](secret_key_separation_followup.md): deriving separate keys for the session cookie and cursors from `CRUCIBLE_SECRET_KEY`.
- [Enum columns declared as strings](enum_columns_followup.md): multi-row inserts that PostgreSQL refuses for some enum columns.

## Deferred designs

- [Batch lookup and the `unique_id` to `mfid` transition](mfid_naming_transition.md)
- [`idtype` to `resource` rename](idtype_to_resource_rename.md)
- [Project archiving](project_archiving.md)
- [User deactivation](user_deactivation.md)
- [Skipped route backlog](skipped_routes.md)

These describe proposed or deferred work, not routes or schema that exist. Confirm current behavior in OpenAPI and the implementation before building a client against a proposal.

## Architecture reference

- [Permission system design](permission_system_plan.md): the role model, historical migration design, and remaining future-work analysis. Use the contract documents above for current HTTP behavior.
