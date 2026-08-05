# Schema Hygiene and FK Conventions

Table-level schema issues found while auditing the permission path for `docs/permission_system_plan.md`. Deliberately **not** part of that redesign — this is cleanup with its own migration risk, and bundling it would make an already-large change harder to review. Item 1 is the exception worth watching: it directly affects the "resources I own" query that the permission model relies on in place of personal projects.

## 1. `owner_orcid` / `owner_user_id` duplication

`Dataset` and `Sample` each carry **both** an owner reference:

```python
owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
owner_orcid:   Optional[str] = Field(default=None)   # no index, no FK
```

Neither is documented as authoritative, and they drift — `samples.py:181` lazily backfills `owner_user_id` when it finds it null, which means the indexed column is *sometimes* unpopulated while the unindexed one is set.

The practical consequence: **the indexed column is unreliable and the reliable column is unindexed.** A "resources I own" query (`GET /datasets?owner_orcid=<me>`) filters the string column and therefore sequentially scans. This matters because that query is the mechanism replacing personal projects (see Projects → Personal workspaces).

The fix is *not* to index `owner_orcid`. It is to make `owner_user_id` trustworthy — backfill it, populate it on every write path — and then resolve ORCID → `user.id` in the route and filter on the existing integer index. No schema addition required.

## 2. `Sample.project_id` has neither index nor FK

```python
# Dataset — indexed and constrained
project_id: Optional[str] = Field(default=None, index=True, foreign_key="project.project_id")
# Sample — neither
project_id: Optional[str] = None
```

`GET /samples?project_id=X` is a sequential scan today. There is no rationale for the asymmetry; Sample should match Dataset.

## 3. No composite index for the dominant listing query

`WHERE project_id = X ORDER BY unique_id DESC LIMIT n` forces the planner to choose between scanning the `project_id` index and sorting everything, or walking `unique_id` and filtering. A composite `(project_id, unique_id)` on `Dataset` serves both — filtered, already ordered, early-terminating under `LIMIT`.

## 4. FK conventions — three variants in use

| Convention | Used by |
|---|---|
| Integer surrogate (`user.id`, `dataset.id`, `accessgroup.id`) | link tables, `AssociatedFile.dataset_id`, `UserAccessGroupLink`, `owner_user_id` |
| String business key (`idtype.mfid`, `user.orcid`, `project.project_id`, `accessgroup.group_name`) | `ResourceACL`, `ScientificMetadataBinary`, `DeletionRequest.resource_id`, `AccessGroupJoinRequest`, `IngestionRequest`, `Dataset.project_id` |
| Unconstrained string, no FK | `owner_orcid` (both tables), `Sample.project_id`, `DeletionRequest.requester_id` |

`UserProjectLink` mixes the first two within a single table — `user_id` → `user.id` (int), `project_id` → `project.project_id` (string).

The mixture is not wrong in itself; there is a defensible rule that explains most of it, and it is worth stating so new tables do not invent a fourth variant:

- **Internal joins use integer surrogate keys.** Narrow indexes, cheap comparison.
- **API-visible identity uses the business key, FK-constrained.** The API speaks mfids and ORCIDs, so using them directly avoids a join on every serialization. `ResourceACL.mfid` is correct for exactly this reason: it must resolve uniformly across datasets, samples, and instruments, which an integer FK to any single table could not do.
- **One relationship, one column.** Denormalize only deliberately; if you do, maintain the copy on every write path and document which side is authoritative.

By that rule the real violations are narrow: the `owner_orcid`/`owner_user_id` pair (item 1), `Sample.project_id` (item 2), and `DeletionRequest.requester_id` being an unconstrained string where the newer `AccessGroupJoinRequest.requester_id` correctly FKs to `user.orcid`.
