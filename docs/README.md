# Documentation index

The deployed OpenAPI document is authoritative for the HTTP contract. `GET /health/ready` identifies the deployed source commit and database schema revisions. This index distinguishes current guidance from deferred designs and historical rollout records.

## Current API and client guidance

- [Client migration summary](client_migration_summary.md): consolidated downstream client actions and compatibility behavior.
- [GET query contract](get_query_contract.md): collection filters, sorting, pagination, anchors, facets, and compatibility behavior.
- [Permission system API changes](permission_system_api_changes.md): role, access, ownership, membership, and authorization contracts.
- [Resource identifier routes](resource_identifier_routes.md): canonical MFIDs, human-readable lookups, and compatibility aliases.
- [Database deletion contract](database_deletion_contract.md): reviewed deletion and direct force-delete behavior.
- [Dataset instrument assignment](dataset_instrument_assignment.md): creation, PATCH compatibility, and atomic reassignment behavior.
- [Instrument access](instrument_access.md): maintainers on the instrument record, the service-account operator group, and dataset access.
- [Instrument and project identifier changes, October 2026](instrument_and_slug_changes_2026-10.md): client handoff for live instrument names, instrument maintainers, and case-insensitive project slugs.
- [Resource timestamp policy](resource_timestamp_policy.md): database-owned creation and modification times and the separate producer timestamp field.
- [Graph route migration](graph_routes_migration.md): current graph security behavior and deferred route consolidation.
- [Remote file records](remote_file_records.md): implemented non-GCS file catalog behavior and deferred resource-generic attachment and file-identifier naming.
- [Route package conventions](../app/api/routes/readme.md): route module and shared dependency guidance.

## Operations and accepted data follow-ups

- [Hub authentication overview](hub_auth_overview.md): sign-in, the five API tables, refresh, logout, and cleanup.
- [Hub login integration](hub_login_design.md): implemented contract and single-instance pilot requirements.
- [Hub authentication operations](hub_auth_operations.md): secrets, logging, cleanup, and activation gates.
- [Hub authentication rollout](hub_auth_release_rollout.md): recorded deployment state and remaining migration steps.
- [Hub authentication security review](hub_auth_security_review.md): activation blockers, dispositions, and verified controls.
- [Dependency security assessment](dependency_security_assessment.md): advisory reachability and staged upgrade batches.

- [Database migration guidance](../migrations/README.md): current schema and data migration safeguards.
- [Production migration data follow-ups](production_migration_data_followups.md): unresolved legacy instrument references and ownership anomalies from the August 2026 migration.
- [Kubernetes deployment notes](../k8s-conf/README.md): current metrics and PodMonitoring setup.

## Deferred designs and backlog

- [API-key retrieval and rotation](api_key_lifecycle_followup.md)
- [Analysis job route migration](analysis_job_routes_migration.md)
- [`idtype` to `resource` rename](idtype_to_resource_rename.md)
- [Project archiving](project_archiving.md)
- [Secure parallel upload migration](secure_parallel_upload_migration.md)
- [User deactivation](user_deactivation.md)
- [Skipped route backlog](skipped_routes.md)

These documents describe proposed or deferred work. They are not evidence that a route or schema exists. Confirm current behavior in OpenAPI and the implementation before building a client against a proposal.

## Architecture reference

- [Permission system design](permission_system_plan.md): implemented role model, historical migration design, and remaining future-work analysis. Use the client and API contract documents above for current HTTP behavior.

## Historical records

Completed rollout plans, superseded audits, and legacy product designs are retained in [`docs/archive/`](archive/README.md). They must not be used as current operational instructions.
