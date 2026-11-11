# Permissions

Every dataset, sample, project and instrument is a record with an MFID. A record carries a list of grants, each stating that an access group holds a role on that record. Users and service accounts are members of access groups, with a role in each group. A caller's access to a record derives only from the caller's groups: for each group that holds a grant, the caller receives the lower of the caller's role in the group and the group's role on the record, and the highest result across all groups is the effective role. There are no deny rules, so adding a grant never reduces any caller's access.

The pure model is in `app/utils/permissions.py` (`Role`, `Permission`, `Principal`, `effective_role`, `can_grant`). The database and HTTP layer is in `app/utils/auth_utils.py` (`load_principal`, `require_permission`, `require_grant`, `permission_filter`).

## Roles

A single ordered set of five roles (`Role`, Postgres enum `resource_role`) is used both for a member's role in a group and for a group's grant on a record. Each role includes all roles below it.

| Role | Adds |
|---|---|
| `viewer` | Read the record, its metadata, files and links. |
| `contributor` | Add to the record: files and uploads, first scientific metadata, keywords, links to records in the same project. Create datasets and samples in a project. Request deletion of a dataset or sample. |
| `editor` | Change existing values: record fields, metadata overwrite, links across projects, unlinking. On a project, edit its details and manage members. |
| `admin` | Manage grants, publish and unpublish, move the record to another project, assign a dataset's instrument, delete files, review deletion requests, request deletion of a project or instrument, change an instrument's status and service-account bindings. |
| `owner` | Transfer ownership and delete directly with `DELETE /resources/{mfid}?force=true`. An owner's grant cannot be changed or revoked by any caller, only transferred. |

Routes require a `Permission`, and `THRESHOLDS` maps each permission to the lowest role that satisfies it: `read` to `viewer`, `create` to `contributor`, `edit` to `editor`, `delete` and `manage_acl` to `admin`, `hard_delete` and `transfer` to `owner`. Deletion is described in the [database deletion contract](database_deletion_contract.md).

## Access model

### Groups

An access group (`AccessGroup`) has a `group_name` and a `group_type`. Membership is a `UserAccessGroupLink` row holding the member's role in the group. A grant is a `ResourceACL` row holding `(mfid, accessgroup_id, permission)`.

| `group_type` | Named after | Members |
|---|---|---|
| `identity` | The `unique_id` of one user or service account | Only that identity, always at `owner`. Created with the user and never joined. |
| `project` | The project's MFID | Project members, each at their project role. |
| `instrument` | The instrument's MFID | Service accounts bound to the instrument (the operator group). |
| `system` | A reserved name; `public` is the only one in use | No members are stored. Every caller is placed in `public` at `viewer` when the principal is built. |
| `custom` | A name chosen by a platform administrator | Manually managed groups created with `POST /access_groups`. None are created automatically. |

The group type, not the format of the name, determines what a group is, because projects, instruments and service accounts all use MFIDs. Lookups always match `group_name` together with `group_type`.

### Effective role

```
effective role = MAX over your groups that hold a grant on the record of
                     MIN(your role in the group, the group's role on the record)
```

- The minimum allows a grant to be lower than the member's role in the group. Every user is `owner` of their own identity group, so sharing a record with a user at `viewer` gives that user `viewer`, not `owner`.
- The maximum makes ownership independent of project role. A project `contributor` who creates a dataset reaches it at `owner` through their identity group and at `contributor` through the project, and can therefore edit their own records but not those of other members. The check contains no ownership special case.
- No matching group means no access. A missing or unknown role value in the database is read as `viewer`.

`require_permission` applies this rule to one record and returns 403 when the role is insufficient. `permission_filter` applies the same rule to list queries: it selects the caller's groups whose role reaches the threshold and matches a grant at or above the threshold for one of them.

Two mechanisms are outside the formula: platform roles and the service-account ceiling, both described below.

## Grants on record creation

`POST /datasets` and `POST /samples` write these grants (`create_standard_acls` in `app/utils/sql_utils.py`, plus the route):

| Group | Grant | When |
|---|---|---|
| Owner's identity group | `owner` | Always |
| Project group | `admin` | A project is given |
| Instrument operator group | `contributor` | A dataset is assigned to an instrument |
| `public` | `viewer` | The request specifies `public: true` |

- The owner is the caller unless `owner` names another identity. Only a caller with platform role `contributor` or `admin` may create a record for another identity.
- An ordinary caller who names a project must hold `contributor` or higher in it. The project is optional: a record without one is accessible only to its owner and through explicit grants.
- The project is granted `admin` so that project roles apply unchanged. The grant is a ceiling, and the member's project role determines the effective access.
- The caller receives no grant when the caller is not the owner, and platform administrators never receive grants.
- Creation returns 409 if the owner has no identity group or the project has no access group. Missing groups are not recreated.

A project receives exactly one grant: its own group at `owner`. Any authenticated user may create a project and becomes its lead; only an administrator may designate another user as the lead, and a service account is never a lead. Proposal sync (`POST /account/sync-projects`, and the proposal lookup performed when a user is created) adds users only to projects with `proposal_sync` enabled: projects created by an administrator, or for which an administrator enabled it with `PATCH /projects/{project_ref}`. A rename by a non-administrator disables it, so that no user can take a proposal's name and receive its members. A deployment can also reserve a naming scheme with `RESERVED_PROJECT_ID_PATTERN`, a regular expression matched against the whole `project_id`, ignoring case: only an administrator may create a project with a matching `project_id` or rename a project to or from one (403 `project_id_reserved` otherwise), and proposal sync then adds users only to matching projects. `POST /instruments` grants the owner's identity group `owner` and creates the operator group with no grant on the instrument.

## Projects

### Membership

A project's members are the `UserAccessGroupLink` rows of its project group, and that table is the only place membership is stored or read. Because the project's single grant is its own group at `owner`, a member's effective role on the project record equals their project role. On the project's datasets and samples the same role applies, capped by each record's grant to the project (normally `admin`).

| Project role | On the project | On the project's records |
|---|---|---|
| `viewer` | Read details and members | Read |
| `contributor` | The same | Create records, add to existing ones |
| `editor` | Edit details, rename, add or change members up to `editor` | Edit all |
| `admin` | Manage members up to `admin` | Manage grants, move, review deletion |
| `owner` (the lead) | Transfer the project, delete it directly | Same as `admin` |

Any authenticated caller can read a project's basic details. Scientific metadata and the member list are returned only to members.

### The project lead

The lead is the member holding `owner` in the project group. Authorization reads that membership (`is_project_owner`). `Project.project_lead_orcid` and `lead_user_id` are display copies written only at creation and by ownership transfer; `PATCH /projects/{project_ref}` rejects lead fields. A group has at most one `owner` (unique index `one_owner_per_group`), so co-leads hold `admin`. A record has at most one `owner` grant (`one_owner_acl_per_resource`).

### Project identity: MFID and `project_id`

A project has two identifiers. The MFID (`unique_id`) is immutable and is used for all internal references: the project group's name, the project's own grant, and `project_mfid` on datasets and samples. `project_id` is the human-readable slug used in URLs and responses. It is unique case-insensitively, consists of 3 to 25 letters, digits, `_` or `-`, and cannot be `admin`, `read_only`, `public` or the name of any existing access group. An `editor` can rename it with `PATCH /projects/{project_ref}`; no other update is required because responses resolve the slug from the MFID. See [resource identifier routes](resource_identifier_routes.md).

## Instruments and service accounts

- An instrument has two independent mechanisms: grants on the instrument record (owner and maintainers) and the operator group of bound service accounts, which receives `contributor` on every dataset assigned to the instrument. Managing an instrument gives no access to its datasets. See [instrument access](instrument_access.md).
- A service account is a user with `is_service_account` set. The flag grants nothing. New service accounts receive platform role `contributor`, which permits reading and creating everywhere, including creating records owned by a named researcher.
- A service account without platform role `admin` is capped at `contributor` on every record, regardless of its grants or group roles. It cannot edit, delete, manage grants, transfer ownership, hold a project role above `contributor`, or register instruments.
- Granting a service account administrator rights (`PATCH /service_accounts/{service_account_ref}`) requires a human administrator in a recently authenticated browser session, never an API key. The change is recorded in the sign-in history as `role_changed`.

## Platform administrators and elevated mode

- `User.platform_role` is `none`, `contributor`, `support` or `admin`. It is a property of the user, not a group, and is never written as grants. `admin` passes every check; `contributor` passes `read` and `create` checks everywhere; `support` is reserved and grants nothing.
- The `Crucible-Privilege-Mode` header determines whether an administrator's bypass applies to a request. With `normal` the administrator is treated as an ordinary caller with only their own groups. `elevated` is accepted only from a human administrator; any other caller receives 403.
- Without the header, an administrator's API key acts elevated and a browser session acts normal.
- Details, response headers and audit logging are described in [explicit privilege mode](explicit_privilege_mode_design.md).

## Public records

A record is public when it has a grant for the `public` group, and that is the only definition: `is_public` and `public_grant_exists` read the grant, and `set_public` is the only writer. Since every caller is in `public` at `viewer`, a public record is readable by every authenticated caller and is never more than readable, even if the grant row specified a higher role.

`public: true` is accepted when creating a dataset or sample. After creation, visibility changes only through `PUT` and `DELETE /resources/{mfid}/access/public`, which require `admin` on the record; `PATCH` on a dataset or sample rejects `public`. Public access is visibility, not a role: it does not count toward reading the grant list or toward the `role` field in responses.

## Changing access

Grants on a dataset, sample or instrument:

| Route | Requires | Notes |
|---|---|---|
| `GET /resources/{mfid}/access` | Any role on the record other than through `public`, or an elevated administrator | Each entry carries `capabilities` for the caller. |
| `PUT /resources/{mfid}/access/{kind}/{principal}` | `admin` | `kind` is `users` or `projects`; body `{"permission": "viewer"}`. Idempotent. |
| `DELETE /resources/{mfid}/access/{kind}/{principal}` | `admin` | 404 when the principal holds no grant. |
| `PUT`, `DELETE /resources/{mfid}/access/public` | `admin` | Publish and unpublish. |

A caller cannot grant a role above their own, nor change or revoke a grant above their own (`can_grant`). `owner` is never granted or revoked through these routes (409; use transfer). Service accounts cannot manage grants. Two cases return 409 and refer to another route: any grant on a project record (use membership), and a dataset's or sample's grant for its own project (use the move route).

Project membership:

| Route | Requires | Notes |
|---|---|---|
| `GET /projects/{project_ref}/users` | Member | |
| `POST /projects/{project_ref}/users/{user_ref}?role=` | `editor` | Default role `contributor`. 409 if already a member at another role. |
| `PATCH /projects/{project_ref}/users/{user_ref}?role=` | `editor` | |
| `DELETE /projects/{project_ref}/users/{user_ref}` | `editor`, or the member themselves | The owner cannot be removed. |

The same cap applies: both the new role and the member's current role must be at most the caller's own, and `owner` is reachable only through transfer. Users can also request to join with `POST /projects/{project_ref}/join_requests`.

Ownership and project are not editable fields. `PATCH` rejects them, and two routes in `app/api/routes/resources.py` perform the change in one transaction. Both return a preview unless called with `?confirm=true`.

- `POST /resources/{mfid}/transfer_ownership` requires `owner`. For a dataset, sample or instrument, the previous owner's grant is removed and the new owner's identity group receives `owner`, because a remaining grant on a personal group would have no meaning. For a project, the previous lead is demoted to `admin` and remains a member, and the new lead is added to the group if needed. The owner columns on the record are updated in the same step.
- `POST /resources/{mfid}/project` (datasets and samples) requires `admin` on the record and `contributor` in the target project. The previous project's grant is removed, the new project receives `admin`, and `project_mfid` is updated.

A move or transfer revokes nothing other than the one grant it replaces. Explicit shares with users or other projects, the public grant, the instrument grant and the owner (on a move) are all retained. After a move, review the grant list if the old project's members were also given access by another grant. `PUT /datasets/{dataset_mfid}/instrument` follows the same rule: it removes only the outgoing instrument's grant.

## Non-obvious rules

- Groups are named by MFID, never by user-entered text. A slug or display name used as a group name could collide with `public`, a user's identifier or another group and silently merge them. For the same reason, `project_id` and `instrument_name` can be renamed freely.
- An identity group is resolved through its single `owner` membership (`identity_group_of`), not by its name.
- A platform administrator is not a member of any group by default. The bypass is never an effective role: `require_permission` returns no role for it, the `role` field in responses stays empty, and in normal mode the administrator sees only what their own groups grant. Membership of a group named `admin` grants nothing.
- Owners editing their own records is a consequence of the maximum in the formula, not a special case. An ownership shortcut would diverge from it.
- A record that exists but that the caller cannot read returns 403 on direct reads of datasets and samples. Lists, searches, links and graphs omit it without an error, and a few nested lookups (thumbnails, linking by MFID) return 404 "not found or access denied".
- The project's grant on its records is `admin`, so project admins can share project data outside the project and delete files.
- Requesting deletion of a dataset or sample requires only `contributor`; approving it requires `admin`; direct deletion requires `owner`.
- A role listed on a grant is a ceiling. To determine a user's access, combine it with the user's role in that group, or query `GET /users/{user_ref}/datasets/{dataset_mfid}`.
- Any listing of a caller's own groups must skip the injected `public` membership (`Principal.real_group_names`).
- API-key callers are cached per worker for `AUTH_CACHE_TTL` seconds (60 by default), so a membership or platform-role change can take that long to take effect.
