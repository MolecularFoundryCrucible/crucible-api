# Documentation index

The deployed OpenAPI document is authoritative for the HTTP contract. `GET /health/ready` identifies the deployed source commit and database schema revisions. This index distinguishes current guidance from deferred designs and historical rollout records.

## Current API and client guidance

- [Client migration summary](client_migration_summary.md): consolidated downstream client actions and compatibility behavior.
- [Permission system API changes](permission_system_api_changes.md): role, access, ownership, membership, and authorization contracts.
- [Resource identifier routes](resource_identifier_routes.md): canonical MFIDs, human-readable lookups, and compatibility aliases.
- [Database deletion contract](database_deletion_contract.md): reviewed deletion and direct force-delete behavior.
- [Dataset instrument assignment](dataset_instrument_assignment.md): current PATCH compatibility behavior and the deferred reassignment operation.
- [Graph route migration](graph_routes_migration.md): current graph security behavior and deferred route consolidation.
- [Remote file records](remote_file_records.md): implemented non-GCS file catalog behavior and deferred resource-generic attachment.
- [Route package conventions](../app/api/routes/readme.md): route module and shared dependency guidance.

## Operations and accepted data follow-ups

- [Database migration guidance](../migrations/README.md): current schema and data migration safeguards.
- [Production migration data follow-ups](production_migration_data_followups.md): unresolved legacy instrument references and ownership anomalies from the August 2026 migration.
- [Kubernetes deployment notes](../k8s-conf/README.md): current metrics and PodMonitoring setup.

## Deferred designs and backlog

- [Analysis job route migration](analysis_job_routes_migration.md)
- [`idtype` to `resource` rename](idtype_to_resource_rename.md)
- [Project archiving](project_archiving.md)
- [Secure parallel upload migration](secure_parallel_upload_migration.md)
- [Skipped route backlog](skipped_routes.md)

These documents describe proposed or deferred work. They are not evidence that a route or schema exists. Confirm current behavior in OpenAPI and the implementation before building a client against a proposal.

## Architecture reference

- [Permission system design](permission_system_plan.md): implemented role model, historical migration design, and remaining future-work analysis. Use the client and API contract documents above for current HTTP behavior.

## Historical records

Completed rollout plans, superseded audits, and legacy product designs are retained in [`docs/archive/`](archive/README.md). They must not be used as current operational instructions.
