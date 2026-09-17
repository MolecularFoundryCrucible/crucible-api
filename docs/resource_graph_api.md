# Resource graph API

## Operation

`GET /resources/{resource_mfid}/graph` returns a bounded graph rooted at a readable sample or dataset. Missing, deleted, inaccessible, project, and instrument roots return 404.

The legacy graph routes remain available and unchanged.

## Query parameters

- `depth`: maximum selected-edge distance from the root, from 0 through 3. The default is 1.
- `direction`: lineage traversal direction, one of `both`, `upstream`, or `downstream`. The default is `both`.
- `relationship_kind`: repeatable `lineage` or `association` filter. Omitting it includes both kinds.
- `relationship_type`: repeatable `is_derived_from`, `is_part_of`, or `unspecified` lineage filter. `unspecified` selects stored null values. Supplying this filter without including the lineage kind returns 422.

Every traversed relationship consumes one depth. Direction applies only to lineage. Associations are undirected and may be traversed from either endpoint. Relationship-kind and relationship-type filters affect both traversal and returned edges.

## Nodes

Every node includes:

- `mfid`
- `resource_type`
- nullable `name`
- shortest `traversal_depth` from the root
- nullable `has_more_relationships`
- nullable compact `project` reference

Sample nodes also include nullable `sample_type`. Dataset nodes also include nullable `measurement`, `data_format`, and compact `instrument` reference.

`has_more_relationships` is evaluated using the request's direction and relationship filters and only considers readable, non-deleted resources. `true` means at least one selected relationship leads outside the returned node set. `false` means the node's selected relationships were fully evaluated and all endpoints are present. `null` means a server limit prevented a definitive per-node result.

## Edges

Lineage edges use parent MFID as `source_mfid` and child MFID as `target_mfid`, regardless of traversal direction. `relationship_type` describes the child relative to the parent and is `is_derived_from`, `is_part_of`, or null.

Association edges are undirected and use sample MFID as `source_mfid` and dataset MFID as `target_mfid`. Their `relationship_type` is always null.

Edge IDs are deterministic composite identities:

```text
lineage:sample:{parent_mfid}:{child_mfid}
lineage:dataset:{parent_mfid}:{child_mfid}
association:sample:{sample_mfid}:dataset:{dataset_mfid}
```

Changing a lineage relationship type does not change its edge ID.

## Bounds and completeness

The server returns at most 500 nodes and 1,000 edges. These are server-owned limits and are included in every response.

- `requested_depth` is the requested maximum distance.
- `returned_depth` is the greatest node depth returned.
- `has_more` reports readable selected topology beyond the returned graph.
- `truncated` reports that a node or edge limit prevented a complete response for the requested depth.
- `truncation_reasons` may contain `node_limit`, `edge_limit`, or both.

Nodes are ordered by traversal depth, resource type, and MFID. Edges are ordered by kind, source MFID, target MFID, and relationship type.

## Authorization

The API authorizes the root and every candidate endpoint before traversal. It does not return or traverse through inaccessible or actively deleted resources. Completeness metadata is calculated only from topology the caller may read.

## Incremental expansion

Clients should normally request `depth=1`. If a boundary node has `has_more_relationships=true`, the client may request that node as a new root and merge nodes and edges by their stable identities. No continuation token is required for this workflow.

## Initial contract boundaries

The initial operation does not accept client-selected node or edge limits, resource-type traversal filters, continuation tokens, or graph revision validators. The server-owned bounds and reroot-and-merge expansion keep the first contract bounded without adding stateful traversal semantics.

## Database deployment

Revision `5c8e1a4d7b20` adds concurrent indexes on `samplelink.parent_sample_id` and `datasetsamplelink.dataset_id` for reverse traversal. It contains no data backfill and may run while the previous API version remains available.
