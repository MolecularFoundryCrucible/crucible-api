# Resource graph

This page describes the links between samples and datasets, the bounded graph route `GET /resources/{resource_mfid}/graph`, and the whole-graph routes used by nano-crucible.

## Links

Samples and datasets have two kinds of relationship:

- **Lineage**, between two samples or two datasets, from parent to child. The optional `relationship_type` describes the child relative to the parent: `is_derived_from`, `is_part_of`, or null when unspecified.
- **Association**, between a sample and a dataset, undirected and without a type.

| Route | Purpose |
|---|---|
| `POST`, `DELETE /samples/{parent_sample_mfid}/children/{child_sample_mfid}` | Create or remove sample lineage; `POST` accepts `relationship_type` |
| `POST`, `DELETE /datasets/{parent_dataset_mfid}/children/{child_dataset_mfid}` | Create or remove dataset lineage; `POST` accepts `relationship_type` |
| `POST`, `DELETE /datasets/{dataset_mfid}/samples/{sample_mfid}` | Create or remove an association |
| `GET /samples/{child_sample_mfid}/parents`, `GET /samples/{parent_sample_mfid}/children` | Readable parents or children; filterable by `relationship_type` |
| `GET /datasets/{child_dataset_mfid}/parents`, `GET /datasets/{parent_dataset_mfid}/children` | Readable parents or children; filterable by `relationship_type` |
| `GET /resources/{mfid}/links` | Immediate lineage and associations of one record |
| `GET /samples?dataset_mfid=`, `GET /datasets?sample_mfid=` | Associated records as a filtered collection |

Creating a link between records of the same project requires `contributor` on the parent (the dataset, for an association) and read access to the other record; across projects it requires `editor` on both. Linking a record to itself returns 422 `self_link`, and a lineage link that would create a cycle returns 409 `lineage_cycle`. Re-creating an existing lineage link without `relationship_type` keeps the stored type. Records pending deletion cannot be linked (409 `pending_deletion`). `GET /datasets/{dataset_mfid}/samples` was removed in 4.0.0; use `GET /samples?dataset_mfid=`.

## Bounded graph

`GET /resources/{resource_mfid}/graph` returns a bounded graph rooted at a readable sample or dataset. A missing, pending-deletion or unreadable root, or a project or instrument, returns 404.

| Parameter | Values | Default |
|---|---|---|
| `depth` | 0 to 3: maximum number of relationships from the root | 1 |
| `direction` | `both`, `upstream`, `downstream`; applies to lineage only | `both` |
| `relationship_kind` | Repeatable `lineage`, `association` | Both |
| `relationship_type` | Repeatable `is_derived_from`, `is_part_of`, `unspecified` (stored null); lineage only | All |

Every traversed relationship consumes one level of depth. Associations can be traversed from either end. The kind and type filters apply to both traversal and returned edges. `relationship_type` without `lineage` in `relationship_kind` returns 422.

### Nodes

Every node has `mfid`, `resource_type`, nullable `name`, `traversal_depth` (shortest distance from the root), nullable `has_more_relationships`, `expansion_availability` and a nullable compact `project` reference. Sample nodes add nullable `sample_type`; dataset nodes add nullable `measurement`, `data_format` and a compact `instrument` reference.

`expansion_availability` has three booleans, `upstream_lineage`, `downstream_lineage` and `associations`, evaluated whatever kinds and direction the request selects. Each is `true` when at least one readable relationship in that channel is absent from the response. Lineage availability respects the `relationship_type` filter. `has_more_relationships` aggregates the channels selected by the request.

### Edges

Every edge has `id`, `kind`, `directed`, `source_mfid`, `target_mfid` and `relationship_type`. Lineage edges are directed from parent (`source_mfid`) to child (`target_mfid`) whatever the traversal direction. Association edges are undirected, with the sample as `source_mfid`, the dataset as `target_mfid`, and a null `relationship_type`.

Edge IDs are stable and do not change when a lineage relationship type changes:

```text
lineage:sample:{parent_mfid}:{child_mfid}
lineage:dataset:{parent_mfid}:{child_mfid}
association:sample:{sample_mfid}:dataset:{dataset_mfid}
```

### Limits and completeness

A response holds at most 500 nodes and 1,000 edges, reported in `node_limit` and `edge_limit`. The limits are fixed by the server.

- `root_mfid` is the root, `requested_depth` the requested depth, and `returned_depth` the greatest node depth returned.
- `has_more` reports readable relationships beyond the returned graph.
- `truncated` reports that a limit prevented a complete response for the requested depth, and `truncation_reasons` contains `node_limit`, `edge_limit` or both.

Nodes are ordered by traversal depth, resource type and MFID; edges by kind, source MFID, target MFID and relationship type.

### Authorization

The root and every candidate node are authorized before traversal. The route never returns or traverses through a record the caller cannot read or a record pending deletion, and completeness fields count only relationships the caller can read.

### Incremental expansion

Clients request `depth=1`, offer parent, child or association expansion from `expansion_availability`, re-root at the selected node with the matching filters, and merge nodes and edges by MFID and edge ID. The route accepts no client-selected limits, resource-type filters or continuation tokens.

## The older graph routes

`GET /entity_graph_cte/{entity_mfid}?recursive={bool}` returns the records linked to one sample or dataset, or with `recursive=true` everything connected to it. `GET /project_graph/{project_ref}` returns all samples and datasets of a project, connected or not. Both return the NetworkX node-link shape (`directed`, `multigraph`, `graph`, `nodes`, `edges`) without the bounds above. nano-crucible, its `tree` command and Crucible Labs use them; neither is deprecated in 4.0.0.

- The starting dataset or sample must be readable and not pending deletion; otherwise the route returns 404. An unknown project returns 404.
- Traversal visits only readable samples and datasets that are not pending deletion, and never crosses a record the caller cannot read to reach one beyond it.
- A project graph holds only the readable samples and datasets of that project that are not pending deletion.
- Platform administrators in elevated mode skip the access checks, but records pending deletion stay hidden.
