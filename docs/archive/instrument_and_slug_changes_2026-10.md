# Instrument and project identifier changes, October 2026

Status: Implemented on branch `feat/instrument-maintainers-and-name-resolution`, targeting `staging`. Not yet deployed. Database changes run only when an operator applies them.

This document summarizes the change for downstream clients and agents (Crucible Hub, Nano Crucible, ingestion clients). The detailed contracts live in [instrument access](../instrument_access.md), [dataset instrument assignment](../dataset_instrument_assignment.md), and [resource identifier routes](../resource_identifier_routes.md).

## Summary

1. Dataset `instrument_name` is resolved live from the assigned instrument. Instrument names are free-text display, never identifiers.
2. Instrument maintainers can be listed through instrument reads, and callers can list instruments they maintain.
3. Project slugs (`project_id`) are matched case-insensitively everywhere, like instrument slugs.
4. Instrument audit records carry more fields, and lifecycle changes are recorded.

## Identifier rules

| Identifier | Role | Matching |
|---|---|---|
| `unique_id` (MFID) | Stable identifier for projects, instruments, datasets, samples | Exact |
| `project_id` | Human-readable project slug | Case-insensitive, unique ignoring case |
| `instrument_id` | Human-readable instrument slug | Case-insensitive, unique ignoring case |
| `instrument_name` | Free-text display label | Not an identifier; may repeat |

Use the MFID to store references. Use slugs for human lookup. Never use `instrument_name` to identify an instrument.

## 1. Instrument display names

### Response behavior

Every dataset representation now returns `instrument_name` from the assigned instrument through `instrument_mfid`, consistent with the flat `instrument_id` and nested `instrument` reference. Renaming an instrument is reflected immediately on all its datasets. Datasets without a registered instrument (`instrument_mfid` null, about 1,144 legacy rows in production) return their stored label.

Affected responses:

| Route | Field |
|---|---|
| `GET /datasets/{dataset_mfid}` | `instrument_name` |
| `GET /datasets` | `items[].instrument_name` |
| `GET /datasets/search` | `items[].instrument_name` |
| `POST /datasets` | `instrument_name` |
| `PATCH /datasets/{dataset_mfid}` | `instrument_name` |
| `GET /datasets/{dataset_mfid}/parents`, `/children` | `items[].instrument_name` |
| `GET /resources/{dataset_mfid}` | `instrument_name` |
| `GET /samples/{sample_mfid}`, `GET /samples` with embedded datasets | `datasets[].instrument_name` |

### Deprecated name-based lookups

These remain supported for compatibility and are marked deprecated in OpenAPI. Clients should migrate.

| Route | Deprecated input | Behavior | Use instead |
|---|---|---|---|
| `POST /datasets` | body `instrument_name` | Consulted only when `instrument_id` and `instrument_mfid` are both omitted. Unregistered name: 400. Name shared by more than one instrument: **409** (previously a 500). | `instrument_mfid` or `instrument_id` |
| `GET /datasets` | query `instrument_name` | Matches the displayed name: the instrument's current name, or the stored label for legacy datasets. | `instrument_mfid` |

`PATCH /datasets/{dataset_mfid}` still accepts the deprecated `instrument_name` only as the unchanged value. Both the stored label and the instrument's current name are accepted (case-insensitive). Any other value returns 409; use `PUT /datasets/{dataset_mfid}/instrument` to reassign.

### Project slug validation

`POST /projects` and `PATCH /projects/{project_ref}` no longer reject a `project_id` that equals an instrument's display name.

## 2. Instrument maintainers

An instrument has two independent access mechanisms. Instrument roles never grant access to datasets recorded with the instrument.

| Mechanism | Members | Controls |
|---|---|---|
| Instrument record ACL | Owner and maintainers (people or projects) | Instrument details, status, service-account bindings, further grants |
| Instrument operator group | Bound service accounts only | `contributor` on every dataset assigned to the instrument |

### Managing maintainers (existing routes)

| Route | Purpose | Requires |
|---|---|---|
| `PUT /resources/{instrument_mfid}/access/users/{user_ref}` with `{"permission": "editor"\|"admin"}` | Add or change a maintainer | `admin` on the instrument; cannot grant above own role |
| `DELETE /resources/{instrument_mfid}/access/users/{user_ref}` | Remove a maintainer | `admin` on the instrument |
| `GET /resources/{instrument_mfid}/access` | Full ACL listing | `admin` on the instrument |
| `POST /resources/{instrument_mfid}/transfer_ownership` | Change owner (preview, then `?confirm=true`) | `owner` |

`user_ref` accepts an ORCID, service-account MFID, username, or email.

| Role | Edit details (`PATCH /instruments/{mfid}`) | Change status (`POST /instruments/{mfid}/status`) | Bind service accounts | Grant access |
|---|---|---|---|---|
| `editor` | yes | no | no | no |
| `admin` | yes | yes | yes | up to `admin` |
| `owner` | yes | yes | yes | up to `admin` |

### New: list maintainers on instrument reads

| Route | New parameter |
|---|---|
| `GET /instruments/{instrument_mfid}` | `include_members=true` |
| `GET /instruments?instrument_id={slug}` | `include_members=true` (requires `instrument_id`; otherwise 422) |

Response field `members` is a list of `AccessGrantRead`, ordered from `owner` down:

```json
"members": [
  {"principal_id": "0000-0001-...", "principal_type": "user", "permission": "owner", "slug": "jdoe", "display_name": "Jane Doe"},
  {"principal_id": "0000-0002-...", "principal_type": "user", "permission": "editor", "slug": null, "display_name": "Sam Lee"}
]
```

Visibility: returned to any caller holding a role on the instrument record and to platform administrators. Everyone else receives `members: null`, including bound service accounts and callers who can read the instrument only through public access. The operator group, public access, and system groups are never listed. `members` is null when not requested. `GET /resources/{instrument_mfid}` does not expand members.

### New: list instruments a caller manages

| Route | Parameter |
|---|---|
| `GET /instruments` | `affiliation=maintainer` |
| `GET /instruments/search` | `affiliation=maintainer` |

`maintainer` matches instruments where the caller's own identity holds `editor` or `admin`. `owner` matches ownership. Repeated values combine with OR: `?affiliation=owner&affiliation=maintainer` lists every instrument the caller owns or maintains.

### Operator group (service accounts)

| Route | Requires |
|---|---|
| `GET /instruments/{instrument_mfid}/service_accounts` | `admin` on the instrument |
| `POST /instruments/{instrument_mfid}/service_accounts/{service_account_mfid}` | `admin` on the instrument |
| `DELETE /instruments/{instrument_mfid}/service_accounts/{service_account_mfid}` | `admin` on the instrument |

Only service accounts can be bound. The instrument owner and admin maintainers can manage bindings (earlier documentation incorrectly said platform administrators only). A bound service account reaches every dataset assigned to the instrument at `contributor`; if its key is exposed, rotate it with `POST /service_accounts/{unique_id}/rotate_key` (platform administrators) or unbind it.

Behavior changes:

- Deprecated `POST /users/{user_id}/access_groups/{group_name}` now returns **409** when adding a human user to an instrument group. Service accounts are still accepted.
- `POST /instruments` no longer grants the operator group `editor` on the instrument record. This grant had no practical effect beyond letting bound service accounts attach scientific metadata to the instrument record.

## 3. Case-insensitive project slugs

`project_id` is now matched case-insensitively, consistent with `instrument_id`. Responses return the slug with its stored casing.

| Route | Input |
|---|---|
| `GET /projects` | query `project_id` |
| `GET /projects/{project_ref}` and every `/projects/{project_ref}/...` route | path slug (compatibility alias; MFIDs remain exact) |
| `POST /projects` | duplicate check on body `project_id` |
| `PATCH /projects/{project_ref}` | duplicate check on renamed `project_id` |
| `GET /datasets`, `/datasets/search`, `/datasets/facets` | query `project_id`, `accessible_to_project` |
| `GET /samples`, `/samples/search`, `/samples/facets` | query `project_id`, `accessible_to_project` |
| `GET /projects` | query `accessible_to_project` |
| `POST /datasets`, `POST /samples` | body `project_id` |
| `POST /resources/{mfid}/project` | body `project_id` |
| `PUT`/`DELETE /resources/{mfid}/access/projects/{project_id}` | path `project_id` |
| `POST /access_groups/{group_name}/join` | path project slug |
| `GET /join_requests` | query `group_name` |
| `GET /project_graph/{project_id}` | path slug |

Behavior changes:

- `?project_id=proj-a` finds a project stored as `Proj-A`.
- Creating or renaming a project to a slug that differs from another project's slug only by case returns **409**.
- A project may rename itself to a case variant of its own slug (`proj_old` to `Proj_Old`).

`POST /datasets` and `POST /samples` with both `project_id` and `project_mfid` accept a slug that matches ignoring case.

## 4. Instrument audit records

The versioned GCS `record.json` for instruments now includes `instrument_id`, `location`, `description`, `instrument_type`, and `status`. `POST /instruments/{instrument_mfid}/status` now writes an audit record (it previously wrote none).

## OpenAPI changes

- New schema `InstrumentAffiliation` (`owner`, `maintainer`) for instrument `affiliation`. Dataset, sample, and project collections keep `Affiliation` (`owner` only).
- New query parameter `include_members` on `GET /instruments/{instrument_mfid}` and `GET /instruments`.
- New response field `InstrumentResponse.members` (`AccessGrantRead[] | null`).
- `DatasetCreate.instrument_name` and the `GET /datasets` `instrument_name` query are marked `deprecated: true`.

## Client actions

| Client need | Action |
|---|---|
| Store or link an instrument | Use `instrument.unique_id` (MFID) |
| Look up an instrument from user input | `GET /instruments?instrument_id={slug}` |
| Create a dataset for an instrument | Send `instrument_mfid` or `instrument_id`, not `instrument_name` |
| Filter datasets by instrument | `GET /datasets?instrument_mfid={mfid}` |
| Show an instrument's maintainers | `GET /instruments/{mfid}?include_members=true`; handle `members: null` |
| "My instruments" view | `GET /instruments?affiliation=owner&affiliation=maintainer` |
| Manage maintainers | `PUT`/`DELETE /resources/{mfid}/access/users/{user_ref}`; use `capabilities.can_manage_access` to gate the UI |
| Look up a project from user input | `GET /projects?project_id={slug}`; any casing works |
| Compare `instrument_name` values | Do not; names may change and repeat |

## Compatibility risk

Low. Two observable changes:

- Dataset responses may show a different `instrument_name` than before for datasets whose instrument was renamed after creation. This is the intended correction.
- Projects whose slugs differ only by case can no longer be created. An audit on 2026-10-05 found no such projects or instruments on staging or production.

## Deployment and database changes

Merging to `staging` deploys code only. Nothing below runs automatically.

1. Migration `b7e2d9c4a1f3` adds case-insensitive unique indexes on `project.project_id` and `instrument.instrument_id`. It aborts if case-only duplicates exist and builds the indexes concurrently. After applying it, set `EXPECTED_DB_REVISIONS` to `b7e2d9c4a1f3` in that environment's configmap (`k8s-conf/configmap-staging.yaml`, `k8s-conf/configmap-main.yaml`), otherwise readiness checks fail. The code works with or without the indexes; the indexes only close the concurrent-create race.
2. Optional: `migrations/data/20261005_remove_instrument_group_record_grants.py` removes the obsolete operator-group `editor` grant from existing instruments. Dry run by default; `--commit` to apply, plus `--yes-this-is-production` on production.
3. Read-only audit used before the migration: `migrations/data/20261005_audit_case_insensitive_slugs.py`.
