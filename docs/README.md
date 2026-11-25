# Documentation

The [OpenAPI document](https://crucible.lbl.gov/api/v4/docs) served by each deployment is the authoritative reference for routes and schemas. The pages below describe the behavior and design decisions that the schema does not express.

## Deployment and operations

| Page | Description |
|---|---|
| [crucible-selfhost](https://github.com/MolecularFoundryCrucible/crucible-selfhost) | Installing and running your own Crucible, with the Docker Compose bundle |
| [Upgrading from 3.3.1 to 4.0.0](upgrading/3.3-to-4.0.md) | Upgrade checklists for Kubernetes, Docker Compose and client authors |
| [Authentication configuration](sign_in_operations.md) | Identity providers, OAuth clients, passwords, administrative commands |
| [Database migrations](../migrations/README.md) | Applying migrations, schema changes, data corrections |

## API concepts

| Page | Description |
|---|---|
| [Authentication](sign_in.md) | OAuth flows for client applications, sessions, API keys |
| [Permissions](permissions.md) | Roles, access groups, projects and how access is evaluated |
| [Explicit privilege mode](explicit_privilege_mode.md) | Opt-in elevation of administrator privileges per request |
| [Resource identifiers](resource_identifiers.md) | MFIDs, slugs and path parameter naming |
| [Collection queries](collection_queries.md) | Filtering, sorting, pagination and facets on list endpoints |
| [Deletion](deletion.md) | Deletion requests and the behavior of records pending deletion |
| [Join requests](join_requests.md) | Requesting and reviewing project membership |
| [Resource graph](resource_graph.md) | Provenance and links between records |
| [Timestamps](timestamps.md) | Creation and modification times |

## Instruments and file storage

| Page | Description |
|---|---|
| [Instrument access](instrument_access.md) | Instrument members, operators and service accounts |
| [Dataset instrument assignment](dataset_instrument_assignment.md) | Assigning and reassigning a dataset's instrument |
| [Uploads](uploads.md) | Signed multipart uploads and storage backends |
| [Storage locations and downloads](storage_downloads.md) | Permitted storage locations and signed download links |
| [File replacement](file_replacement.md) | Replacing the content of a stored file and restoring a previous version |
| [Remote file records](remote_file_records.md) | Cataloging files held outside the API's storage |
| [Workers](workers.md) | Worker registration, the advertised ingestors and ingestion warnings |
