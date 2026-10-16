# Graph route migration

Status: Deferred compatibility cleanup. The current security behavior below is implemented, but the proposed canonical graph routes and removal of duplicate routes are not.

## Current client contract

Nano Crucible is the graph API entry point used by the CLI, Graph Explorer, and Crucible Labs. It currently calls:

```text
GET /entity_graph_cte/{resource_mfid}?recursive={bool}
GET /project_graph/{project_ref}
```

Crucible Lens links to Graph Explorer and does not call a graph API route directly. No current external caller was found for the other graph routes.

The two compatibility routes must remain available during the client migration. They accept the existing parameters and return the existing NetworkX node-link representation with `nodes` and `edges`.

## Current security rules

- The starting dataset or sample must be readable and must not have a pending or approved deletion request. An inaccessible starting resource returns `404`.
- Entity traversal may visit only readable, active samples and datasets. It cannot cross an inaccessible resource to reveal another visible resource beyond it.
- Project graphs include only readable, active samples and datasets.
- Project graph references accept either the project MFID or slug.
- Platform administrators bypass ACL checks but active deletion requests remain hidden.

## Proposed client migration

Introduce these canonical resource-scoped routes in a separately reviewed compatibility release:

```text
GET /resources/{resource_mfid}/graph?recursive={bool}
GET /projects/{project_ref}/graph
```

Keep the existing routes as deprecated wrappers around the same internal graph service. Do not duplicate traversal or authorization logic in wrappers.

Update clients in this order:

1. Add the canonical routes and deterministic response models to the API.
2. Update Nano Crucible to use the canonical routes.
3. Verify Graph Explorer, the Nano CLI, and Crucible Labs through Nano.
4. Keep the compatibility wrappers for one documented release window.
5. Remove the compatibility wrappers and unused graph implementations in the next versioned API contract.

## Removed duplicate routes

Removed on 2026-10-16:

```text
GET /entity_graph/{entity_id}
GET /entity_graph_bfs/{entity_id}
GET /samples/{sample_id}/sample_graph
GET /samples/{sample_id}/sample_graph_cte
GET /datasets/{dataset_id}/dataset_graph
GET /datasets/{dataset_id}/dataset_graph_cte
GET /projects/{project_ref}/sample_graph
GET /projects/{project_ref}/dataset_graph
GET /projects/{project_ref}/entity_graph
```

The API's Prometheus request counters (retained from 2026-03-26) showed no call to any of them after 2026-08-01, and four were never called. Neither nano-crucible nor crucible-labs calls them; crucible-labs used the three project routes until 2026-05-28 (`b8576f6`). Several of them traversed through unreadable or pending-deletion datasets before filtering, or returned unreadable parent MFIDs.

`GET /entity_graph_cte/{entity_mfid}` does not return `relationship_type` on its edges; `GET /resources/{resource_mfid}/graph` does.

## Open design decisions

- Whether recursive traversal may cross project boundaries when both endpoints and every intermediate resource are readable. The compatibility route currently allows this.
- Whether project graph access requires READ on the project itself or only returns individually readable resources belonging to the project. The compatibility route currently uses individual resource access.
- Whether graph responses should continue using the NetworkX node-link shape or adopt a dedicated versioned response with explicit node and edge models.
