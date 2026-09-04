# Schema Hygiene and FK Conventions

Archived status: This audit records pre-permission-migration findings. Several schema and query assumptions were superseded by the August 2026 migration. Use it only as historical context and verify unresolved concerns against the current models.

Table-level schema issues found while auditing the permission path for `docs/permission_system_plan.md`. Deliberately **not** part of that redesign - this is cleanup with its own migration risk, and bundling it would make an already-large change harder to review. Item 1 is the exception worth watching: it directly affects the "resources I own" query that the permission model relies on in place of personal projects.

## 1. `owner_orcid` / `owner_user_id` duplication

`Dataset` and `Sample` each carry **both** an owner reference:

```python
owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
owner_orcid:   Optional[str] = Field(default=None)   # no index, no FK
```

Historically these columns drifted because sample creation only populated `owner_user_id` for some owner shapes. Current dataset and sample creation resolves the canonical user once and writes both columns before ACL creation. Ownership transfer also updates both atomically, and the migration backfill repairs historical rows where possible.

The practical consequence: **the indexed column is unreliable and the reliable column is unindexed.** A "resources I own" query (`GET /datasets?owner_orcid=<me>`) filters the string column and therefore sequentially scans. This matters because that query is the mechanism replacing personal projects (see Projects → Personal workspaces).

The query path should use the indexed `owner_user_id`: resolve the public identity reference to `user.id`, then filter on the integer column. `owner_orcid` remains the canonical public response value and denormalized compatibility pointer. No additional index is required.

## 2. `Sample.project_id` has neither index nor FK

```python
# Dataset - indexed and constrained
project_id: Optional[str] = Field(default=None, index=True, foreign_key="project.project_id")
# Sample - neither
project_id: Optional[str] = None
```

`GET /samples?project_id=X` is a sequential scan today. There is no rationale for the asymmetry; Sample should match Dataset.

## 3. No composite index for the dominant listing query

`WHERE project_id = X ORDER BY unique_id DESC LIMIT n` forces the planner to choose between scanning the `project_id` index and sorting everything, or walking `unique_id` and filtering. A composite `(project_id, unique_id)` on `Dataset` serves both - filtered, already ordered, early-terminating under `LIMIT`.

## 4. FK conventions - three variants in use

| Convention | Used by |
|---|---|
| Integer surrogate (`user.id`, `dataset.id`, `accessgroup.id`) | link tables, `AssociatedFile.dataset_id`, `UserAccessGroupLink`, `owner_user_id` |
| String business key (`idtype.mfid`, `user.orcid`, `project.project_id`, `accessgroup.group_name`) | `ResourceACL`, `ScientificMetadataBinary`, `DeletionRequest.resource_id`, `AccessGroupJoinRequest`, `IngestionRequest`, `Dataset.project_id` |
| Unconstrained string, no FK | `owner_orcid` (both tables), `Sample.project_id`, `DeletionRequest.requester_id` |

`UserProjectLink` mixes the first two within a single table - `user_id` → `user.id` (int), `project_id` → `project.project_id` (string).

The mixture is not wrong in itself; there is a defensible rule that explains most of it, and it is worth stating so new tables do not invent a fourth variant:

- **Internal joins use integer surrogate keys.** Narrow indexes, cheap comparison.
- **API-visible identity uses the business key, FK-constrained.** The API speaks mfids and ORCIDs, so using them directly avoids a join on every serialization. `ResourceACL.mfid` is correct for exactly this reason: it must resolve uniformly across datasets, samples, and instruments, which an integer FK to any single table could not do.
- **One relationship, one column.** Denormalize only deliberately; if you do, maintain the copy on every write path and document which side is authoritative.

By that rule the real violations are narrow: the `owner_orcid`/`owner_user_id` pair (item 1), `Sample.project_id` (item 2), and `DeletionRequest.requester_id` being an unconstrained string where the newer `AccessGroupJoinRequest.requester_id` correctly FKs to `user.orcid`.

## 5. Resources not reachable through a project-organised view

The permission plan replaces personal projects with a "My data" view (resources whose owner is the caller). That covers the common case, but a UI that organises everything by project plus one "My data" tab still cannot reach every resource. The cases, so none is discovered by a user first:

| Case | Reachable via | Notes |
|---|---|---|
| `project_id IS NULL` | "My data" | The intended case. Works once `owner_user_id` is reliable (item 1). |
| Shared directly with you, you are not in the owning project | neither | Reached through an ORCID-group ACL entry; belongs to no project *you* can see and is not owned by you. Needs a "Shared with me" view. |
| Public resources from projects you are not in | neither | Same shape. Needs the `visibility=public` filter the permission plan adds. |
| Owned by a departed user, still in the project | project view | Fine, listed for completeness - the resource stays put when its owner leaves. |
| `project_id` names a project that does not exist | **neither** | A defect, not a view gap. See below. |

The last row follows from item 2: `Sample.project_id` has no foreign key, so nothing prevents a dangling value. Such a sample looks filed - the field is populated - but appears in no project listing, and if its owner is also unset it appears nowhere at all. `Dataset.project_id` is FK-constrained and cannot reach this state.

Count them before the permission migration, since the ACL backfill keys off project membership:

```sql
SELECT count(*) FROM sample s
LEFT JOIN project p ON p.project_id = s.project_id
WHERE s.project_id IS NOT NULL AND p.project_id IS NULL;
```

Adding the FK in item 2 requires this to be zero, so the count is needed either way.
