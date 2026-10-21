# Batch lookup and the `unique_id` to `mfid` transition

Status: Phase 1 implemented 2026-10-21 (branch `feat/batch-mfid-filters`). Phase 2 and later items are agreed direction but not scheduled; do not start them without a decision.

## Goals

1. Let clients fetch up to 100 records of one type in a single request, through the existing typed collections instead of a separate resolve endpoint.
2. Retire `POST /users/resolve` in favor of filters on `GET /users`.
3. Rename the resource identifier from `unique_id` to `mfid` on the wire without breaking existing clients, then rename the storage to match.
4. Later: a mixed-type `GET /resources` summary collection, and MFIDs for human users.

## Phase 1: repeatable identifier filters

- `GET /datasets`, `GET /samples`, `GET /projects`, `GET /instruments` accept a repeatable `mfid` query parameter, at most 100 values (422 above that). Results use the collection's normal representation, authorization, pending-deletion hiding, sorting, and pagination; unreadable, missing, and pending-deletion MFIDs are simply absent. Results are not in request order; clients key them by `mfid`.
- `GET /datasets` and `GET /samples` keep `unique_id` as a deprecated alias of `mfid` (repeatable too). Sending both is 422. Projects and instruments never had the filter, so they get `mfid` only.
- `GET /users` makes `unique_id` and `username` repeatable (at most 100) and keeps `email` as an exact, case-insensitive filter available to every caller. Exact filters, single or repeated, keep their existing meaning: they search every user rather than only users who share a group, and return the same representation as `GET /users/{user_ref}`.
- `POST /users/resolve` is marked deprecated (with a `Deprecation` header), and removed after the Hub and nano-crucible (`crucible/resources/users.py:232`) have moved. Today it lets any authenticated user resolve any user by email without the directory scoping `GET /users` applies.

## Phase 2: `mfid` on the wire (expand and contract)

### Current naming

The API already uses `mfid` in places, so the target name is established, but the surface is mixed:

| Concept | Names in use today |
|---|---|
| A record's own MFID | `unique_id` (16 resource schemas), `mfid` (graph nodes, `AssociatedFileRead`, `ResourceACL`, print) |
| A reference to another record's MFID | `*_mfid` (most), `resource_id` (deletion, reassignment, transfer), `dataset_id` and `sample_id` (`DatasetSampleLinkResponse`) |
| Internal integer ids exposed | `dataset_id` on `AssociatedFileRead` and `ThumbnailRead`; `parent_dataset_id`, `child_dataset_id`, `parent_sample_id` on link models |
| Path parameters | already `{mfid}`, `{dataset_mfid}`, `{sample_mfid}`, ... |

Target convention: `mfid` for a record's own identifier, `<type>_mfid` for references, and no internal integer ids in responses.

### Scope

Resource schemas with `unique_id`: `DatasetCreate`, `DatasetRead`, `DatasetResponse`, `SampleCreate`, `SampleRead`, `SampleResponse`, `ProjectRead`, `ProjectWithLead`, `ProjectReferenceRead`, `InstrumentResponse`, `InstrumentReferenceRead`, `LinkedResource`, `ResourceSearchResult`, `ScientificMetadataBinaryRead`. Reference fields to rename in the same pass: `resource_id` on `DeletionRequestRead`, `DeletionAuditLogRead`, `DeletionPreviewRead`, `ReassignProjectRead`, `TransferOwnershipRead`, and `dataset_id`/`sample_id` on `DatasetSampleLinkResponse`. Query parameters: `unique_id` on `/datasets` and `/samples` (Phase 1).

Out of scope: user and service-account schemas (`UserRead`, `UserPublicRead`, `UserCreate`, `AccountProfileRead`, `AccessGroupMemberRead`, `ServiceAccount*`), where `unique_id` is an ORCID for humans, and user reference fields (`owner_orcid`, `requester_id`, `reviewer_id`, `principal_id`, `user_unique_id`, `project_lead_orcid`). Those wait for user MFIDs. The internal integer ids are a separate breaking change; list them in the same deprecation notice but replace them with `*_mfid` fields rather than renaming.

Not affected: message queue payloads (ingestion and postprocessing use `dsid` and `dataset_mfid`), pagination cursors (they encode values, not field names), the `sort=unique_id` value on `GET /users`.

### Steps

1. **Expand.** Every in-scope response carries `mfid` alongside `unique_id` with the same value; `unique_id` is marked `deprecated` in OpenAPI (Pydantic `Field(deprecated=...)`). Request bodies accept either name and reject both (422). Requests that use a deprecated name get a `Deprecation` response header (RFC 9745) and increment a counter so the remaining use can be measured. Reference fields gain their `*_mfid` names the same way.
2. **Migrate clients.** The Hub regenerates its TypeScript types and moves to `mfid`. nano-crucible moves in a release. Other repositories that read `unique_id` from responses: `crucible-ingestion`, `crucible_graph_explorer`, `crucible-upload-uis`, `crucible-lens`, `crucible-labs`, `crucible-rabbit-mq`, `nano-client-api-e2e`, plus user notebooks and scripts we cannot see.
3. **Contract.** Remove `unique_id` from in-scope responses, bodies, and filters. Response field use cannot be measured server-side, and nano-crucible is installed from PyPI so old versions keep running, so this step is gated on a minimum supported nano-crucible version and an announced date, not on traffic. Expect months, and treat it as a breaking API release.

### Storage

No client reads the database directly (checked: none of the sibling repositories open a database connection), so storage names are internal and independent of the wire contract.

- **ORM attributes.** Rename `unique_id` to `mfid` on `Dataset`, `Sample`, `Project`, `Instrument`, and `ScientificMetadataBinary` while keeping the column name, the same technique `User.unique_id` already uses for the `orcid` column (`sa_column=Column("unique_id", ...)`). About 870 references in `app/` and 950 in `tests/`; mechanical, no schema change, no deploy coordination.
- **`idtype` to `resource`.** See [`idtype_to_resource_rename.md`](idtype_to_resource_rename.md). Its "hard cutover" can be avoided: rename the table and create a view named `idtype` over it in the same migration, so pods still on the old image keep working during the rollout; drop the view in the next release. The table's key column is already `mfid`.
- **Column renames** (`unique_id` to `mfid` on the four typed tables). `ALTER TABLE ... RENAME COLUMN` is metadata-only, but Postgres has no column alias, so pods on the old image fail for the length of the rollout. Either accept a short coordinated cutover with `lock_timeout`, or skip it: with the ORM attribute renamed, the column name is invisible everywhere except raw SQL. Recommendation: skip unless there is a reason beyond tidiness.
- **GCS record files** (`app/utils/gcs_record_utils.py`) write `unique_id` into archived JSON. They are a record format, not the API; keep the key or write both, but do not rewrite existing objects.

## Later

- **`GET /resources`**: a cursor-paginated mixed-type collection returning a `ResourceSummary` (`mfid`, `resource_type`, `name`, `identifier`, `project_mfid`, timestamps), filterable only on fields every type shares (`mfid`, `resource_type`, `project_mfid`). It must join the typed tables (production has about 37.5k `idtype` rows without one) and must not grow type-specific filters, which belong on the typed collections. Build it when a mixed-type use appears that the typed collections cannot serve.
- **MFIDs for human users**: one identifier scheme for owners, grants, audit, and graph nodes; ORCID becomes a linked identity alongside institutional or other sign-in for self-hosted deployments; account merges and ORCID re-linking stop being key changes; ORCIDs leave URLs and logs. ORCID stays a visible attribute for provenance. Large migration (`owner_orcid` and every user reference); pair it with the storage renames above.
