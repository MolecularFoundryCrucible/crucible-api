# Permission System Redesign

Status: The permission model and production migration are implemented. This document preserves architecture decisions and deferred work; it is not an operational checklist. The rollout phases later in the document are historical design context and no longer describe the current environment. The completed procedure is retained in `docs/archive/permission_migration_runbook_2026-08.md`.

## Goals

The current permission model is binary: read or write access to a group's resources. This is too coarse for a platform where researchers, collaborators, facility staff, instrument software, and operators all have different levels of responsibility.

The redesign adds five roles (`viewer`, `contributor`, `editor`, `admin`, `owner`). A role appears in two places — your standing in a group, and what that group was granted on a resource — and your effective rights are the weaker of the two. Platform admin access is a separate bypass outside the ACL system entirely.

Two goals drive the specifics:

1. **Different roles within a project.** A project viewer and a project admin should not have identical access to the project's data.
2. **Service accounts without administrative authority.** A service account must be able to read and contribute project data without editing existing values, deleting resources, managing ACLs, transferring ownership, or administering the platform.

---

## Core Concepts

### Resources

A resource is anything with an MFID: datasets, samples, instruments, **and projects**. Every resource has ACL entries in `resourceacl` naming which groups may access it and at what role.

### Projects become resources

Projects are not resources today — `create_project()` writes no `Resource` row, so every project's `creation_time` and `modification_time` is null and a project cannot carry ACL entries, scientific metadata, or a deletion request the way a dataset can.

An earlier draft deferred fixing this. It is in phase 1 instead, because the rest of the model gets simpler rather than harder once it holds:

- A project has an MFID, so it stops being identified solely by the mutable `project_id` handle (see Future Work → Project identity).
- A project's own ACL entry replaces `Project.project_lead_orcid` as the definition of who leads it ("Project lead" becomes a role, below).
- Project metadata edits become an ordinary `require_permission` check instead of a bespoke one.
- `creation_time`/`modification_time` get populated by the same path as every other resource, so projects become visible to the incremental-sync mechanism.

**A project gets exactly one ACL entry: its own group, at `owner`.**

Because effective role is `MIN(standing, grant)` and the grant is `owner`, effective role on the project resource is simply the member's standing in the project group. The lead holds `owner`, admins hold `admin`, a `contributor` sits below the `editor` bar and therefore cannot rename the project. No second entry, no lookup table, no synchronisation.

The migration backfills a `Resource` row per existing project. The one thing to audit is code that assumes every `Resource` row is a dataset, sample, or instrument — the deletion filter in particular.

### ACL entries at resource creation

What a newly created dataset or sample actually gets. Created by U, owned by O, in project P, on instrument I:

| Group | Today | Proposed | Applies to |
|---|---|---|---|
| O's ORCID group | read+write | `owner` | both |
| P's project group | read+write | `admin` | both, when `project_id` given |
| I's instrument group | read+write | `contributor` | **datasets only**, when `instrument_name` given |
| U's ORCID group, if U≠O | read+write | **dropped** | — |
| `admin` | read+write | **dropped** — bypass | — |
| `read_only` | read-only | **dropped** — unused, not replaced | — |

Roughly six rows becomes three. Three of those changes carry reasoning worth keeping:

**The ACL grant is a ceiling, not an assignment.** Because effective role is `MIN(standing, grant)`, the value here caps what the group's members can reach. The primary project is granted `admin` so that project roles apply at face value — granting `editor` instead would silently demote project admins on their own project's data, which is its only effect. The same column expresses sharing intent elsewhere: a project a resource is shared *into* gets `viewer` or `editor`, a real cap.

Note what `admin` on the primary project unlocks beyond deletion review: project admins can delete member resources directly (both paths write a `DeletionAuditLog` row) and can manage ACL entries, which includes sharing project data outward. That last point is what the deferred project-privacy setting addresses.

**The instrument group gets `contributor`, not `editor`, on datasets.** The grant records the instrument association and remains useful if service-account access is narrowed in the future. Service accounts with `platform_role=contributor` already receive platform-wide contributor access, so this row does not elevate them.

**The requestor entry is dropped**, for every caller with no exceptions. It was redundant once create-on-behalf must name a project — the creator reaches the resource through that project group. Keeping it meant a service account accumulating a personal ACL entry on every dataset it ever created, thousands of rows expressing nothing. "Who created this" is audit-log information, not an access grant.

No requestor ACL is required for service accounts because their contributor access is evaluated without per-resource rows.

Samples never receive an instrument entry. For datasets the instrument entry is re-written on update when `instrument_name` changes (`datasets.py:252`).

### Roles

Five roles, declared in ascending order:

```sql
CREATE TYPE resource_role AS ENUM ('viewer','contributor','editor','admin','owner');
```

A Postgres enum rather than `VARCHAR`, deliberately: enums compare by declaration order, so `role >= 'editor'` and `LEAST(a, b)` work natively in SQL. With `VARCHAR` every ordering comparison in the list-filter path would need a `CASE` expression.

| Role | Read | Create | Edit others' | Delete | Manage ACL | Transfer ownership |
|---|---|---|---|---|---|---|
| `viewer` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `contributor` | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `editor` | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| `admin` | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| `owner` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

`owner` differs from `admin` in exactly two ways, and they are the reason both exist: only `owner` can transfer ownership, and an owner's ACL entry cannot be modified by an `admin`. Everything else is identical.

### Effective role

A `Role` value lives on two different columns for two different things: `useraccessgrouplink.role` is your **standing** in a group; `resourceacl.permission` is what that group was **granted** on a resource. Staging briefly introduced both as `role` and then renamed the ACL column. Production starts from a schema with neither column and adds the final names directly. The `Permission` enum (`READ`/`CREATE`/.../`TRANSFER`, used by `has_permission`/`require_permission`) is a third, unrelated thing: an action, not a grant level.

```
effective_role(user, resource) =
    MAX over every group G the user belongs to that has an ACL entry on the resource:
        MIN( user's role in G , G's granted role on the resource )
```

The `MIN` is what lets a grant be *weaker* than the recipient's standing. Without it, sharing is broken: an individual share adds an ACL entry for the recipient's ORCID group, and since everyone holds `owner` in their own ORCID group, "share this dataset with Alice as a viewer" would grant Alice owner rights, including delete.

The `MAX` is what makes ownership work. Consider a `contributor` in project P creating dataset D:

| Path | role in group | ACL grant on D | min |
|---|---|---|---|
| Alice's ORCID group | `owner` | `owner` | **owner** |
| Project P's group | `contributor` | `editor` | `contributor` |

`MAX` = `owner`. She owns what she creates regardless of her project standing, and keeps it if she later leaves the project.

**"Contributors can manage their own resources" is therefore not a rule — it is a consequence.** Editing someone else's dataset leaves you at your project role (`contributor`, below the `editor` bar); editing your own gives you the `owner` path. No `is_it_mine?` branch exists anywhere in the permission check. A reader expecting a special case will go looking for one, so it is worth stating plainly.

This is the single most load-bearing behaviour in the model, and the easiest to break by accident — an `is_owner` shortcut added later "for clarity" would silently diverge from the `MAX` path. The exhaustive `effective_role` matrix in Testing must cover it directly: a `contributor` on their own resource, a `contributor` on a peer's, and the same pair after the owner leaves the project.

### Permission thresholds

| Operation | Requires |
|---|---|
| read | `>= viewer` |
| create in a project | `>= contributor` in the project group |
| edit an existing resource | `>= editor` |
| delete | `>= admin` |
| manage ACL entries | `>= admin`, and only at or below your own role |
| transfer ownership | `owner` |
| reassign a resource's project | `>= admin` on the resource, `>= contributor` in the target project |

The last two rows are **not** reachable through `PATCH`. See Reassignment below.

### Design invariants

Two properties that must hold, stated so they are not accidentally broken later:

- **No deny rules.** The model is monotonic — adding a grant can never reduce anyone's access. Deny rules are where ACL systems stop being auditable.
- **Bypass access is never materialized.** Platform-wide access lives in the check function, never as per-resource ACL rows (see Platform Access).

### Model rationale

The two-axis design is a deliberate single level of indirection between "who the user is" and "what the object is." The alternative — one axis — forces a group per (object-type × permission-level) combination, the classic `report-readers` / `report-writers` proliferation. Collapsing this back to one axis to "simplify" it would reintroduce that, and would break sharing as described above.

### Identifiers

Every entity should carry four things, each with one job. `User` already does; `Project` and `Instrument` each miss a piece, and the missing pieces are what make group naming unsafe.

| | internal join key | immutable global id | human handle | display name |
|---|---|---|---|---|
| **User** | `id` | `unique_id` (ORCID) | `username` | `first_name` / `last_name` |
| **Project** | `id` | *missing* → MFID | `project_id` | `title` |
| **Instrument** | `id` | `unique_id` (MFID) | *missing* | `instrument_name` |

**Project needs an MFID.** Phase 1 already creates it, since making projects resources means an `idtype` row.

**Instrument needs a handle.** `instrument_name` is currently doing two incompatible jobs: it is the display name *and* the access-group name. It cannot be both — 10 of 43 registered instruments contain spaces (`b30 - aja sputter tool`, `in situ uv-vis`), which are fine in a label and wrong in an identifier. Add `instrument_id` alongside, validated like `project_id`, and let `instrument_name` go back to being free text. Backfilling 43 rows with slugs is a small job.

What each one is for:

- **`id`** — internal joins only, never in an API payload. Narrow index, cheap comparison.
- **MFID** — what never changes. Foreign keys that must survive a rename, and **all access-group names**. An MFID cannot collide with `public`, an ORCID, or another entity's handle, which is what makes the escalation described under Currently Unguarded Operations structurally impossible rather than merely forbidden.
- **Handle** — what a human types and what appears in a URL. Unique, validated, and *mutable*: renaming must be possible without touching a single foreign key, which is precisely what naming groups after MFIDs buys.
- **Display name** — free text, no constraints, no uniqueness. Never load-bearing.

The rule that follows: **nothing derived from user input ever becomes an identifier.** Handles are validated but never referenced internally; MFIDs are referenced internally but never typed.

`ResourceACL` is the one deliberate exception to "join on `id`" — it keys on `idtype.mfid` because it must resolve uniformly across datasets, samples, instruments and now projects, which no integer FK to a single table could do. That trade is already made and has not caused a measurable problem.

### Groups

| Group type | Name | Members |
|---|---|---|
| `project` | Project MFID | Project members, at their project role |
| `identity` | `User.unique_id`, either a human ORCID or service-account MFID | Exactly that user, always at `owner` |
| `instrument` | Instrument MFID | Bound instrument service accounts |
| `system` | Reserved name such as `admin`, `read_only`, or `public` | Defined by the system policy rather than generic membership routes |
| `custom` | Administrator-chosen legacy name | Legacy compatibility only |

**Every entity-owned group has an explicit `group_type`.** Identifier format is not a discriminator because service accounts, projects, and instruments all use MFIDs. Human and service-account personal groups are both `identity`; the owning `User.unique_id` determines the member. Project and instrument groups use the corresponding entity MFID. Reserved literals are `system` groups.

> **Migration note.** Revision `91f3c8a4d2e7` adds nullable `accessgroup.group_type`. `20260827_backfill_access_group_types.py` classifies existing rows only after detecting identifier collisions, missing entity-owned groups, and malformed identity membership. Revision `c4a7e9d2b615` then makes the column non-null. ACL rows are unaffected because they reference `accessgroup.id`.

### Public resources

`Resource.public` is replaced by an ACL entry against a virtual `public` group. Publishing a resource writes one row: `(resource, public_group, viewer)`. Every `Principal` is constructed with that group appended at `viewer`.

The group is **virtual**: the `accessgroup` row exists so ACL rows can reference it, but there are no `useraccessgrouplink` rows. Nobody is added to it — the principal builder injects it.

**Why this rather than a boolean.** A flag forces a branch into the permission check, and the natural one-liner is catastrophically wrong:

```python
if resource.public:
    return True          # grants edit and delete, not just read
```

As a group, that mistake is unrepresentable. Every principal's standing in `public` is `viewer`, and effective role is `MIN(standing, grant)`:

```
min(viewer, viewer) = viewer
min(viewer, admin)  = viewer      ← even a mis-written ACL row
min(viewer, owner)  = viewer
```

**A resource cannot be made world-writable, structurally** — not by a bug in the check, not by a bad row, not by a botched migration. With a flag, that guarantee depends on every code path remembering to treat `public` as read-only; here it is a property of the model.

Three things follow:

- **The `OR` in the access filter disappears.** `public = true OR EXISTS(acl…)` spanned two tables and was already flagged as a planner hazard; it collapses into the ordinary `accessgroup_id IN (:my_groups)` predicate.
- **Publishing inherits the right authorization for free.** It becomes an ACL-management operation, which already requires `admin` on the resource — so the currently ungated `public = true` is closed by construction rather than by adding a check.
- **Anonymous access, if ever wanted** (published data behind a DOI, readable without login), is a principal whose only group is `public`. A boolean would require the whole auth path to tolerate having no user.

**The column goes; the API field stays.** `public: bool` remains on the create, update, and read models so no client changes. On read it is derived:

```sql
EXISTS (SELECT 1 FROM resourceacl
        WHERE mfid = dataset.unique_id AND accessgroup_id = :public_group_id) AS public
```

one probe per row against the covering index, well under a millisecond for a 100-row page. On write, `public: true` is translated by the route into the ACL row. It is deliberately *not* kept as a stored column alongside the ACL row because that would recreate the two-sources-of-truth drift documented in the archived pre-migration schema audit.

**It also collapses an existing inconsistency.** There are currently *two* public flags, and different code paths consult different ones:

| Column | Indexed | Access-filter sites |
|---|---|---|
| `Dataset.public` | yes | 14 — `files.py`, `ingestion_requests.py`, and others |
| `Resource.public` (`idtype`) | no | 3, including `read_access_filter_list` |
| `Sample` — no column | — | samples rely solely on `Resource.public` |

They are kept in step by hand (`ri.public = ds.public` on create and update). Nothing enforces it, so a drift makes a dataset visible through `GET /files` but not `GET /datasets`, or the reverse — an authorization inconsistency, not merely duplicated state. One ACL row replaces both columns and leaves a single code path.

Two edges to handle: `GET /access_groups/public/users` returns empty, since membership is virtual; and anything iterating `principal.groups` — "which projects am I in", for instance — must exclude the public group.

#### Discovering public resources

Injecting the public group means public resources show up in ordinary listings for every caller with no route changes — that part gets easier. What is *lost* is the ability to ask for public resources specifically: today `?public=true` filters a column that will no longer exist.

So the listing routes need an explicit `visibility` filter (`all` default, `public`, `private`), compiling to a predicate on the presence of the public group's ACL row rather than a column. Without it a "browse public data" view has no query behind it, and the natural workaround — clients filtering client-side after paging — breaks under cursor pagination.

#### Which column wins in the migration

`Dataset.public` and `Resource.public` disagree wherever the hand-maintained copy drifted, so the backfill needs a rule. Neither a union nor an intersection is right blind: a union can publish something intended private, an intersection can un-publish something users rely on.

Authority follows what the code actually writes:

| Resource type | Authoritative column | Why |
|---|---|---|
| Dataset | `Dataset.public` | What the dataset routes write and what users toggle; `Resource.public` is the copy |
| Sample | `Resource.public` | No `Sample.public` column exists |

**Run the disagreement count before migrating**, not after — it is one query, and it says whether this is a real problem or a theoretical one:

```sql
SELECT count(*) FROM dataset d JOIN idtype r ON r.mfid = d.unique_id
WHERE d.public IS DISTINCT FROM r.public;
```

If the count is zero the rule is moot and the migration is trivial. If it is not, every row in it is a resource whose visibility is currently ambiguous depending on which route you ask, and each one wants a decision before it is frozen into an ACL entry.

---

## Platform Access

### Platform admins

Platform admins bypass all resource-level checks. This lives on `User`, not in group membership — groups control resource access, user attributes control system-level bypass.

```sql
CREATE TYPE platform_role AS ENUM ('none','contributor','support','admin');
ALTER TABLE "user" ADD COLUMN platform_role platform_role NOT NULL DEFAULT 'none';
```

An enum rather than `is_platform_admin BOOLEAN`, for the same reason `resource_role` is an enum. The doc already anticipates wanting a platform-wide read-only auditor; as a boolean that arrives later as a *second* boolean, and two booleans encoding one tier is how `read_only` became a group with a row on every resource. `support` is reserved now and granted to nobody — the cost over a boolean is zero, and it removes the temptation to reach for a group when the need appears.

It travels on the `Principal` (see Implementation → Auth), so the check needs no extra query:

```python
def require_permission(principal, resource_id, permission='read'):
    if principal.platform_role == 'admin':
        return True
    # effective-role check against resourceacl
```

**Why a user attribute here, when `Resource.public` is being deleted for being a boolean.** The two are opposite cases, and the distinction is the rule worth keeping. `public` was a *per-resource* flag duplicating something `resourceacl` already expressed — a second access path for a question the ACL table answers, which is why it drifted. Platform admin is a *per-principal* fact the ACL table structurally cannot express: it grants access to resources with no ACL entry, including resources that do not exist yet. Encoding it as a group would require an ACL row on every resource — precisely what removing the `admin` group's rows is undoing. Both changes enforce the same invariant: one access path per question.

**Stop materializing bypass access.** `create_standard_acls` currently writes an ACL row for the `admin` group and the `read_only` group on every resource created. The `admin` row is dead weight under this model — platform admins never consult the ACL.

`read_only` is dropped outright rather than converted. Its ACL rows are the only place in the schema where `write=False` appears systematically — removing them is what makes the backfill a single uniform assignment rather than a mapping (see Migration path). If a platform-wide read-only auditor is wanted later, it should be designed as a flag and a bypass, not as a group with a row on every resource, per the invariant above.

> **It is not quite unused.** An earlier draft claimed it was. Production has one member, a developer, who today reads every resource through its 360,505 rows. Unlike the `admin` group there is no bypass for them, so deleting those rows removes any access they hold *only* through it. **Decided: accept the loss** — reinstate something deliberate later if it turns out to matter. Recorded because "unused" was wrong once and the same assumption is what produced the admin-group bug.

Removing both drops per-resource ACL rows from roughly six to three and eliminates two inserts per resource creation.

The `admin` group remains as an organizational container and for route-level checks, but no longer drives the bypass.

### Service accounts

`is_service_account` identifies the principal type and grants no global permission by itself. New service accounts receive `platform_role=contributor`, which grants platform-wide read and create access without ACL materialization. They may read resources, create resources and child records, and use `accessible_to_user` to restrict project, dataset, and sample collections to a named user's effective access. A non-admin service account remains capped at contributor regardless of stronger ACL or group standing, so it does not receive edit, delete, ACL-management, ownership-transfer, instrument-registration, or platform-administration authority.

When a caller creates a resource naming someone else as owner, the server verifies that the named user may create in the target project:

```
caller creates resource with owner_orcid = X, project_id = P
if X != caller:
    require X is a member of P at >= contributor
owner ACL entry -> X's ORCID group at `owner`
```

Authority derives from the researcher, not from the service account, so the service account needs no project membership. Initial metadata creation, file registration, upload completion, keyword association, ingestion-request creation, and same-project resource linking use contributor-level create permission. Updating existing values continues to require editor access.

#### Binding a service account to an instrument

`POST /instruments` provisions the instrument operator group, and `POST` or `DELETE /instruments/{instrument_mfid}/service_accounts/{service_account_mfid}` manages its service-account membership. Dataset creation rejects unregistered instruments instead of creating groups from arbitrary names.

The binding is many-to-many and records which credentials operate each instrument. Under the current platform-wide contributor policy it is provenance and discovery metadata, not an authorization gate for writing project data. This preserves a usable scope boundary if service-account access is narrowed later and supports credential rotation without remodeling the relationship.

> **Instrument-project association remains deferred.** Service accounts use platform-wide contributor access for project discovery and same-project linking, so ingestion does not depend on maintaining instrument-project membership.

---

## Projects

### Project membership

`POST /projects/{proj_id}/users/{orcid}?role=contributor` — defaults to `contributor`. The project lead gets `admin` at creation.

| Role | Can do |
|---|---|
| `viewer` | Read project resources |
| `contributor` | Create resources; edit only their own |
| `editor` | Read and write all project resources |
| `admin` | Full control, manage membership and ACLs |

Routes:
- `POST /projects/{proj_id}/users/{orcid}?role=` — add member
- `PATCH /projects/{proj_id}/users/{orcid}?role=` — change role
- `GET /projects/{proj_id}/users` — list members with roles
- `DELETE /projects/{proj_id}/users/{orcid}` — remove member

> **Existing behaviour this changes.** `POST /projects/{proj_id}/users/{orcid}` is currently gated `require_groups(ag_list, {'admin', proj_id})`, which means **any member of a project can add anyone else to that project** — a viewer can add a collaborator.

#### Adding members requires `editor`, capped below your own role

Restricting membership management to `admin` is the obvious tightening, but it is stricter than the risk warrants, and it puts a senior postdoc onboarding a student through a project lead every time.

Adding a member and managing ACL entries are not the same power, which is what makes a middle ground safe: **adding a member grants access bounded by the project's own grant, while an ACL entry can share data outward to any group, unbounded.** The second is genuinely `admin`-shaped. The first is not.

Project membership follows a strict hierarchy. An `editor` may add or modify `viewer` and `contributor` members, an `admin` may manage `editor` and below, and the `owner` may manage `admin` and below. A role cannot create, promote, demote, or otherwise modify a peer. Removal remains restricted to the project owner, platform administrators, or the member leaving voluntarily.

No sixth role. Every role added multiplies the `effective_role` test matrix, the migration mapping, and the number of things a reader has to hold in their head — and "can add members but not manage ACLs" is expressible with the roles that exist plus a cap that is already written down.

| Operation | Requires |
|---|---|
| add a project member | `>= editor` in the project group, at most your own role |
| change a member's role | `>= editor`, with both the old and new role at most your own |
| remove a project member | `>= editor` and the member's role at most your own, or the member themselves; never the owner |
| manage resource ACL entries | `>= admin` on the resource, at or below your own role |

Changed 2026-10-19: membership now follows the same "at most your own role" rule as resource ACL entries, replacing the strict hierarchy and owner-only removal described above, so one rule covers every grant. Project access is managed only through membership: the resource ACL routes refuse project records, and they refuse to change a dataset or sample's grant for its own project, which follows `POST /resources/{mfid}/project`.

### Membership lived in two tables; one was never authoritative

Found while auditing the routes touched by this redesign, not something the redesign introduced. `UserProjectLink` (`user_id`, `project_id`, FK to `project.project_id`) and `UserAccessGroupLink` (`user_id`, `accessgroup_id`, `role`, matched to a project via `AccessGroup.group_name == project_id`) both claimed to answer "is this user a member of project P." Only the second one is ever consulted for authorization — `authorize_user`'s `load_principal` builds a `Principal`'s groups exclusively from `UserAccessGroupLink`. `UserProjectLink` fed `User.projects`/`Project.users`, `GET /users/{orcid}/projects`, and (until this fix) `GET /projects/{proj_id}/users` — a parallel membership directory kept in sync only by the routes that write both, `sql_add_user_to_project` and `remove_user_from_project`.

**The generic `POST/DELETE /users/{orcid}/access_groups/{group_name}` routes never updated `UserProjectLink` at all** — they predate this redesign and call `sql_add_user_to_access_group` directly. Used against a project's group name, they could grant real ACL-backed access invisible to every membership listing, or revoke real access while a stale `UserProjectLink` row kept reporting the user as a member with none.

**Fixed without a schema change.** `UserAccessGroupLink` joined through `AccessGroup` (still matched on `group_name` for now) is now the sole read path — `is_project_member`/`is_project_group` (`app/utils/project_utils.py`), `sql_get_projects_for_user`, `_get_project_users`, `list_projects`, `read_projects_for_user`, and the join-request "already a member" check all read it. The two generic routes now reject a project's group name outright, pointing callers at `/projects/{proj_id}/users/{orcid}`. `UserProjectLink` is still written (`sql_add_user_to_project`, `create_project`, `create_user`) — legacy, redundant, harmless — and is a phase-5 drop candidate once nothing reads it.

**What `UserProjectLink` had that `UserAccessGroupLink` still lacks: a real FK.** `AccessGroup.group_name == project_id` is string-matching, not a foreign key — nothing stops an orphaned or colliding group name. The proper fix is two mutually-exclusive nullable FKs on `AccessGroup`, one per group kind that actually has a natural referent:

| Group kind | FK | Target |
|---|---|---|
| ORCID group | `owner_orcid` | `user.orcid` (already an FK target elsewhere — `AccessGroupJoinRequest.requester_id`) |
| Project / instrument group | `resource_mfid` | `idtype.mfid` |
| `public`, `admin` | neither | no natural referent — not owned, not resource-backed |

Deliberately **not done now**: `resource_mfid` can't be populated for new projects until `POST /projects` creates the project's own `Resource`/`idtype` row (open, see Routes checklist), and switching the read paths to key off a column that is `NULL` until a backfill runs is the exact deploy-ordering hazard the admin-group-bypass lesson already cost one incident (see Migration path). This belongs in phase 4/5 of the group-C work (MFID-named groups), sequenced with the project-resource-creation fix, not bolted on ahead of it.

**Later update:** this did get built (`owner_orcid`/`resource_mfid` on `AccessGroup`, Group C4a), then reverted after an audit found exactly one real reader across the whole codebase and zero readers of `owner_orcid` at all. The FK-safety argument above is real, but nothing had actually hit the "orphaned or colliding group name" failure it guards against, so the columns were pure classification with no consumer. Removed in `migrations/versions/20260813_bb780697b63b_drop_accessgroup_owner_orcid_and_.py`. Worth reintroducing if a real reader shows up, e.g. the tag-plus-policy model discussed for the scattered-ACL-rows problem, where `resource_mfid` would become load-bearing for computing implicit grants, not before.

### "Project lead" becomes a role

Several routes currently key off `Project.project_lead_orcid` directly: join-request review (`_is_group_lead` in `access_groups.py`), project member removal, and project listing.

**The lead holds `owner` in the project group, not `admin`** — and once projects are resources (above), that is not a convention but the same statement as "the lead owns the project resource." The project's single ACL entry grants its own group `owner`, so effective role on the project equals standing in it:

| Standing in project group | Effective role on the project resource | Can |
|---|---|---|
| `owner` (lead) | `owner` | Everything, including transferring the project |
| `admin` | `admin` | Manage editors and lower roles and ACLs, delete resources; cannot transfer |
| `editor` | `editor` | Edit project metadata and all project resources; manage contributors and viewers |
| `contributor` | `contributor` | Create resources; cannot rename the project |
| `viewer` | `viewer` | Read |

`admin` was the wrong level for a lead because the two roles differ in exactly the ways a lead needs: only `owner` can transfer the project to a successor, and only an `owner`'s ACL entry is safe from modification by an `admin` — without which any project admin could demote the lead.

**`Project.project_lead_orcid` stops being read for authorization, but is not dropped.** An earlier draft of this section called for retiring the column entirely once its readers migrated to a standing check. Revised: `Dataset.owner_orcid`/`owner_user_id` and the sample equivalents are the same shape, a denormalized "who owns this" pointer living alongside ACL-based authorization. Those columns are *not* on the phase-5 drop list; they stay as disciplined, maintained copies under the archived schema audit's rule: "one relationship, one column; if you denormalize, maintain the copy on every write path." Singling out `project_lead_orcid` for deletion instead of the same treatment had no stated reason beyond "keeping both is how the two notions drift," a real risk but the same one already accepted for `owner_orcid` and solved there by discipline rather than deletion.

So `project_lead_orcid` gets the `owner_orcid` treatment:

- **Never read for authorization.** `_is_group_lead`, the project-listing filter, and project member removal move to a real owner-standing check (`is_project_owner()`, querying `UserAccessGroupLink`/`AccessGroup` the same way `is_project_member` does) — this part is unchanged from the original plan.
- **FK'd**, like `AccessGroupJoinRequest.requester_id` already is (`foreign_key="user.orcid"`) — it isn't today, which is its own small inconsistency.
- **Written only by `transfer_ownership`.** `PATCH /projects/{proj_id}` rejects the field outright, same as `owner_orcid`/`project_id` on datasets and samples. `POST /resources/{mfid}/transfer_ownership` updates the ACL/standing and the denormalized pointer atomically, in one transaction — for a project that means demoting the current `owner`-standing holder and promoting the new one within the project's one group; for a dataset/sample it means moving the `owner` ACL entry between the two users' personal ORCID groups, exactly as `owner_orcid`/`owner_user_id` are already the norm there.

Net effect: one item comes off the phase-5 contract list. `list_projects`/`read_project`/`get_resource`'s lead-resolution code (`ProjectWithLead.lead`) never has to change to a live query later — it keeps reading the column, permanently.

#### At most one owner per group

Worth enforcing in the database rather than in application code, and it holds for every group type — a project has one lead, a user owns their own ORCID group, and instrument groups have no owner at all:

```sql
CREATE UNIQUE INDEX one_owner_per_group
    ON useraccessgrouplink (accessgroup_id) WHERE role = 'owner';
```

**Co-PIs sit at `admin`.** A project has exactly one `owner`; every other senior member holds `admin`. The gap between them is deliberately narrow — an `admin` manages members, ACLs, and deletion, and is blocked from precisely two things: transferring the project, and modifying the owner's ACL entry. So a co-PI is unimpeded day to day, and the project always has one unambiguous person accountable for its disposition.

This is also what keeps the constraint enforceable in the database rather than as a policy nobody checks. "The lead is a set" would push that decision into application code on every transfer, deletion, and departure.

### Personal workspaces — no special concept

**Decided: there is no "personal project" type.** Ownership is already automatic and a one-member project already is a personal project, so the concept adds a name and nothing else.

Three points on a spectrum cover the need, all using machinery that exists today:

| Want | Do |
|---|---|
| Capture data without organising it | omit `project_id` — you own it, nobody else sees it |
| Group some measurements under a name | create an ordinary project; you are its only member |
| Collaborate | same project, add members with roles |

The distinction that makes this work is **ownership versus container**. Ownership happens automatically — every resource you create gets your ORCID group as `owner`, project or no project. A container is a name you choose, which is what projects already are. A personal project would only have duplicated the first to provide the second.

A single-member project needs no `is_personal` flag and no separate code path, and it becomes a shared project by adding members — no conversion step. `project_id` is already `Optional` on `DatasetCreate` and `SampleCreate`, and `create_standard_acls` already skips the project ACL when it is absent, so unattached resources work today.

What a personal project would have cost, for that name: a `Project` row per user with a backfill for everyone existing, an `is_personal` column, a `project_id` that is also an ORCID (two namespaces colliding in one unique column), hiding rules so they do not appear in other people's `GET /projects`, a "cannot be deleted" lifecycle rule — and, once projects become resources, a `Resource` row and ACL entries per user as well.

**The one real gap is presentational.** If the UI organises everything by project, unattached data has nowhere to appear. That is a "My data" view, not a table: query resources whose owner is the caller. The archived pre-migration schema audit records the original `owner_user_id` reliability concern.

Unattached resources are not the only thing a project-organised UI cannot show, and the full historical list is retained in the archived pre-migration schema audit.

### Sharing

Primary mechanism: project groups. Adding a user to a project gives them access to all project resources at the weaker of their project role and each resource's grant to that project.

Per-resource sharing is an escape hatch for granting access outside the project:

```
POST   /resources/{id}/share      { "orcid": "0000-...", "role": "viewer" }
DELETE /resources/{id}/share/{orcid}
```

This adds one ACL entry pointing at the recipient's ORCID group at the named role. The `MIN` in the effective-role formula is what makes the named role actually bind.

---

## Instruments

An instrument is a resource with an MFID, an owner, and ACL entries, plus an instrument group holding the machine's operational identity.

### Two concerns, two groups

An instrument is two different things to the permission model, and conflating them is what made earlier drafts of this section awkward:

| Concern | Subject | Grant lives on |
|---|---|---|
| The machine writes data | platform-wide service-account contributor policy | project resources |
| Humans curate the machine's record: name, status, location, calibration | its **manager group** — people | the **instrument resource** |

These are not the same set of principals and never were. A group is a set of principals that should have the *same* access, and standing is a single value per `(user, group)` with no rung below `viewer` — so one group holding both cannot give them different access. Two concerns, two groups.

| Principal | Group | Standing | On the instrument record | On the machine's datasets |
|---|---|---|---|---|
| Service account | `instrument-{mfid}` (operators) | `admin` | capped at `contributor` | `contributor` |
| Instrument manager | manager group — *deferred* | `admin` | `admin` | **none** |
| Registrar | own ORCID group | `owner` | `owner` | none |

The service-account ceiling applies even when an instrument operator group has a higher standing or grant. A non-admin service account cannot edit the instrument, change its status, or manage its ACL entries.

#### The dataset grant records provenance, not current configuration

**This is the point that resolves a problem earlier drafts could not.** The grant on a dataset means *"this machine's operators produced this"* — a historical fact, written once at creation. It is **not** derived from `dataset.instrument_id`.

That distinction matters because the alternative — treating the grant as a projection of the instrument field — creates two copies of one fact that can disagree:

- change a dataset's instrument, and the old grant is stale
- correct a mistyped instrument name, and the grant is wrong
- edit the grant directly, and the instrument field says otherwise

Reading it as provenance removes the duplication. Correcting a dataset's instrument later does not touch its ACL, because who produced it did not change. There is nothing to keep in sync, and the entry appears in `GET /resources/{mfid}/access` as an ordinary grant rather than something read-only that the API has to explain.

#### Why a group rather than the service account directly

The group preserves the instrument-to-credential relationship independently of any one key. It covers a control PC driving several detectors, multiple credentials during rotation, and future policies that may scope service-account access by instrument. It does not elevate a service account above the current contributor ceiling.

#### Instrument managers do not get the datasets

A manager can curate the hardware record and read nothing the machine produced. To reach the data they join the project or receive a share, like anyone else.

**Decided, revisit later.** The alternative — managers get `viewer` on their machine's output — is defensible for troubleshooting, and can be added by granting the manager group on datasets. Starting closed and widening on evidence is the cheaper direction.

What managers keep regardless is `viewer` on *public* datasets, through the virtual public group every principal holds. So the loss is narrower than "no access": private data from their beamline becomes unreachable, public data stays exactly as readable as before. The agreement tests pin both halves.

> **Defer the manager group.** Instrument records are managed today by people who are already platform admins, so they keep working through admin bypass and the operator group is SA-only from day one. Add `instrument-{mfid}-managers` when a beamline scientist needs to edit instrument metadata without being a platform admin. This keeps the migration subtractive: existing instrument groups have their human members removed rather than a new group backfilled.

> **This tightens what staff can see.** Today anyone in an instrument group reads every dataset that machine has produced. Afterwards they see nothing unless they are a project member. Intended, but the kind of change a beamline scientist notices the same week — worth announcing rather than shipping quietly.

#### Creating an instrument does not mint a credential

`POST /instruments` creates the instrument, its resource row and its operator group. It does **not** create a service account.

Any authenticated human may create an instrument and becomes its owner when no owner is supplied. Platform administrators may create on behalf of another registered identity. Service accounts cannot register instruments. The owner's identity group receives `owner` on the instrument resource. The separate operator group holds no grant on the instrument record; it receives `contributor` on datasets assigned to the instrument. See [instrument access](instrument_access.md).

Minting one is a deliberate second step, so a live credential exists only where something is actually writing rather than for every instrument anyone ever registered. `POST /instruments?create_service_account=true` keeps the one-call ergonomics available for the common case without leaving unused keys lying around by default.

### Status lifecycle

Instruments are not deleted; a `status` column tracks lifecycle (`active`, `maintenance`, `decommissioned`). `GET /instruments` defaults to `status=active`. Only instrument `owner`/`admin` or platform admins may change status.

### Dataset ownership

Datasets created by an instrument service account belong to the researcher, not the instrument. The `owner` reference in the payload names the researcher and the `owner` ACL entry is created for them, subject to the create-on-behalf verification above. The deprecated `owner_orcid` compatibility field remains available for exact canonical identity lookup.

---

## Historical authorization gaps

Discovered while mapping existing routes. These take `auth_res` — so authentication is required — but perform **no authorization check at all**. They must be closed as part of this work rather than found one at a time:

| Route | Today | Should require |
|---|---|---|
| `POST /projects` | none | decide: any user, or a registrar role |
| `POST /instruments` | authenticated | caller owns the instrument; only service accounts and platform administrators may create on behalf |
| `PATCH /instruments/{instrument_unique_id}` | none | instrument `admin`/`owner` |
| `POST /datasets`, `POST /samples` | none | `>= contributor` in the target project, plus create-on-behalf verification |
| setting `public = true` | none | resolved by construction — see below |
| `instrument_name` on `POST /datasets` | none | validate against the `Instrument` table |
| `group_name` on `POST /access_groups/{group_name}/join` | none | validate that the group is a project group — see below |
| `project_id` on `POST /projects` | none | format check + reserved names + cross-namespace collision — see below |

The instrument ones are the most exposed: nothing currently prevents any authenticated user from renaming or re-pointing someone else's instrument.

> **Status.** All rows are closed except `POST /projects`'s open policy
> decision (any user, or a registrar role — unchanged) and `setting public =
> true`, resolved by construction once the virtual `public` group ships
> (Phase 5). `PATCH /instruments/{instrument_mfid}` and `POST
> /datasets`/`POST /samples`'s create-on-behalf check were closed earlier in
> this work; `POST /instruments`, `instrument_name` on `POST /datasets`,
> `group_name` on join, and `project_id`'s grammar/collision check are
> closed as of Group C.

### `project_id` collides with the access-group namespace — privilege escalation

**This is a live vulnerability, not a consequence of the redesign.** `create_project` derives the project's access group from the caller-supplied `project_id`:

```python
ag = add_or_update(session, AccessGroup, AccessGroup.group_name == P.project_id, group_name=P.project_id)
if project_lead not in ag.users:
    ag.users.append(project_lead)
```

`add_or_update` **reuses** an existing group rather than rejecting the name, `project_id` has no validation of any kind, and `POST /projects` has no authorization check. So:

- `project_id: "admin"` appends the named lead to the **admin access group**. Every `require_groups(ag_list, {'admin'})` in the codebase then passes. Any authenticated user can make themselves a platform administrator with one request.
- `project_id: "public"` attaches the project to the virtual public group. Under this design every principal is injected there at `viewer`, so the whole platform gains read on that project's resources.
- A `project_id` equal to a user's ORCID or an instrument name silently merges two unrelated groups.

The last case is **not hypothetical**: `unknown` is currently both a project and an instrument. The backfill classifies it as `project` (projects are checked first), so datasets from that instrument grant `admin` rather than `contributor`.

#### The fix is naming, not validation

Validation patches the symptom. The cause is that a group name is derived from user-supplied text. **Group names become MFIDs**:

| Group | Named after | Collision possible? |
|---|---|---|
| Project | `project.unique_id` (the MFID projects gain in phase 1) | no |
| Instrument | `instrument.unique_id` | no |
| ORCID | the ORCID | no |
| `public`, `admin` | reserved literals | not from user input |

An MFID is 26 characters of Crockford Base32; it cannot equal `public`, an ORCID, or another project's handle. Collisions stop being forbidden and start being unrepresentable. This also resolves the instrument case cleanly: `instrument_name` is a **display name**, not an identifier — 10 of 43 registered instruments contain spaces (`b30 - aja sputter tool`, `in situ uv-vis`) — so it should never have been a group name, and forcing it into an identifier grammar would destroy readable labels.

This is the Future Work item "Project identity: mfid vs `project_id`" promoted into scope. It is not a nicety; it closes a privilege-escalation class.

#### `project_id` still needs a grammar

Even once groups are MFID-named, `project_id` remains a URL path segment and a foreign key value, so it needs to be well-formed and unique:

```
^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$
```

Deliberately looser than `_USERNAME_RE` (`^[a-z][a-z0-9_-]{2,31}$`), because the existing data requires it: **6,707 of 6,757** project IDs contain uppercase (the `MFP*` proposal IDs), and 3 begin with a digit. Requiring lowercase or a leading letter would reject almost every real project.

Only **4** existing rows fail the grammar above, and all four are junk worth cleaning up regardless:

| Value | Problem |
|---|---|
| `''` | empty string as a project_id |
| `'[7093]'` | brackets |
| `'Training [9069]'` | brackets and spaces |
| `'SummerSchool DEMO'` | space |

On top of the grammar, reject case-insensitively: the reserved names (`public`, `admin`, `read_only`), any existing user ORCID, and any existing access group belonging to another namespace. Case-insensitively, because `PUBLIC` creating a group distinct from `public` is confusing even where it is not exploitable.

**Join requests accept any group name but can only be approved for projects.** `POST /access_groups/{group_name}/join` takes the name unvalidated, so a request to join an instrument group, another user's ORCID group, or a typo-created orphan is accepted and sits pending. Approval then fails: `review_join_request` calls `sql_add_user_to_project(requester, req.group_name, ...)`, which inserts a `UserProjectLink` whose `project_id` is foreign-keyed to `project.project_id`. For a non-project group that row has no parent, so the reviewer gets a 500 rather than a 403, after the request has already been accepted.

The fix is validation at request time, not a generic add-to-any-group route. Every group type has a membership path that knows its own authorization bar. Projects use `POST /projects/{id}/users/{orcid}`, instruments use the service-account binding route, identity groups are created with the user and never joined, and `public` is virtual. The deprecated generic routes now reject `identity`, `project`, and `system` groups, require an existing group, and temporarily retain instrument and custom compatibility behavior.

This is a live bug independent of the redesign, but it lands in scope because `_is_group_lead` and the whole join-request path are being migrated to standing checks anyway.

**Publishing is the subtlest, and the redesign closes it without adding a check.** Today `public` is a plain pass-through field (`ri.public = ds.public`, `datasets.py:185` and `samples.py:198`) with no gate at all, so any user who can create or edit a resource can make it world-readable — a contributor on confidential or industry-partnered work can publish unilaterally. Once publishing becomes an ACL entry against the `public` group (see Core Concepts → Public resources), it is an ACL-management operation and therefore already requires `admin` on the resource. The gap closes as a consequence of the model rather than as a special case.

**Unvalidated `instrument_name` creates access groups.** `create_dataset` looks the name up in the `Instrument` table (`datasets.py:164`) and sets `instrument_id = None` when there is no match — but then creates an access group from the raw string anyway (`datasets.py:197-201`) and writes an ACL entry for it. So `POST /datasets {"instrument_name": "xrd-typo"}` silently produces a permanent access group named `xrd-typo`. A typo pollutes the group table and attaches a meaningless ACL entry.

The fix is not validation bolted onto `create_dataset` — it is moving group creation to `POST /instruments`, so `create_dataset` only ever looks a group up and can reject an unregistered name outright. That is the same change required to bind service accounts to instruments; see Platform Access → Binding a service account to an instrument.

`POST /projects` deserves a deliberate exception to this list. It has no check today, and that is arguably correct: ad-hoc single-member projects are the intended way to group personal work (see Projects → Personal workspaces). Requiring approval to create one would kill that workflow and push people back toward wanting automatic personal projects. Leave it open, knowingly.

Several other routes show no helper call but do filter inline on `ResourceACL.accessgroup_id.in_(ag_ids)` (e.g. `add_keyword`). Those are checked, just not through the shared helper — they need converging on `require_permission`, not fixing.

---

## Reassignment Never Revokes

Verified, and it affects both resource types. `PATCH /datasets/{dsid}` (`datasets.py:238-252`) and `PATCH /samples/{sample_id}` (`samples.py:121-127`) **add** an ACL entry when the owner, project, or instrument changes, and never remove the previous one:

| Change | Result today |
|---|---|
| move dataset from project P to Q | Q gains access; **P keeps full access indefinitely** |
| change `owner_orcid` from Alice to Bob | Bob gains `owner`; **Alice stays `owner`** |
| change `instrument_name` | new instrument group gains access; old one keeps it |

So reassignment grants without revoking. The owner case is the sharpest: PATCHing `owner_orcid` is the closest thing to an ownership transfer the API has, and it leaves the resource with two owners and no way to remove the first.

### The fix: reassignment is not a field edit

The bug is not that the revoke was forgotten. It is that these operations were modelled as field assignments in the first place.

`PATCH {"owner_orcid": "..."}` reads like a metadata correction, but it is a multi-table state transition: the new owner's ACL entry, the old owner's ACL entry, `owner_user_id`, `owner_orcid`, and `modification_time` all have to move together. It also carries a different authorization bar from the rest of the payload — editing a description needs `editor`, handing the resource to someone else needs `owner`. A single route that mixes both cannot express that in one check, which is exactly how the missing revoke slipped in.

So these two fields leave `PATCH` entirely and become explicit transitions:

| Route | Requires | Does |
|---|---|---|
| `POST /resources/{mfid}/transfer_ownership` | `owner` on the resource | Moves the `owner` ACL entry, updates `owner_user_id`/`owner_orcid`, touches `modification_time` — atomically |
| `POST /resources/{mfid}/project` | `admin` on the resource, `>= contributor` in the target project | Adds the new project's ACL entry, removes the old one |

`PATCH /datasets/{dsid}` and `PATCH /samples/{sample_id}` reject `owner_orcid` and `project_id` with `422` and a pointer to the right route. The instrument case stays on `PATCH` — an instrument group grant is not ownership, so re-pointing it is an ordinary edit — but it must revoke the old group's entry, which it currently does not. **Deferred**: tracked in the Routes checklist, not yet fixed.

Three properties come out of this that the field-edit version could not provide: the revoke lives in one place per transition rather than being implied by an assignment; the authorization bar is stated per transition instead of averaged across a payload; and each transition is one obvious place to write a `DeletionAuditLog`-style record when permission-change auditing lands (Future Work).

The cost is two new routes and a client-visible break for anyone currently PATCHing `owner_orcid` — worth knowing about, though that path is silently broken today anyway.


---

## Implementation

### Schema

Split by migration phase (see Migration path → Environments and rollout). Nothing in the expand list is destructive; everything in the contract list is, which is why they ship separately.

**Expand — phase 1, additive only:**

- [ ] `CREATE TYPE resource_role AS ENUM ('viewer','contributor','editor','admin','owner')`
- [x] Add `contributor` to `platform_role`, yielding `('none','contributor','support','admin')`
- [x] `useraccessgrouplink` -- add `role resource_role`, backfill it, then enforce `NOT NULL`
- [x] `resourceacl` -- add `permission` as `resource_role`, backfill it, then enforce `NOT NULL`
- [x] `resourceacl` -- add covering index `(accessgroup_id, mfid, permission)`
- [x] `user` -- add `platform_role platform_role NOT NULL DEFAULT 'none'`
- [ ] `instrument` — add `status VARCHAR DEFAULT 'active'`
- [ ] `accessgroup` — insert the virtual `public` group row
- [ ] `idtype` — backfill a `Resource` row per existing project, `resource_type='project'`
- [x] `useraccessgrouplink` -- add partial unique index `one_owner_per_group` after every standing is assigned
- [x] `resourceacl` -- add partial unique index `one_owner_acl_per_resource` after every permission is assigned; this permits zero owners but rejects multiple owners
- [ ] `project` — add FK `project_lead_orcid` → `user.orcid` (not destructive; needs a precondition check first — see "Project lead becomes a role")

**Contract — phase 5, after the new path is trusted:**

- [ ] `resourceacl` — drop `read`/`write` **and their indexes**
- [ ] `dataset` — drop `public` **and its index** (bare boolean index, no selectivity)
- [ ] `idtype` — drop `public`

### Indexing

> Table-level schema issues found during this audit are retained in `docs/archive/schema_hygiene_pre_permission_migration.md`. Verify each item against the current schema before treating it as open work.

Row-count is not the scaling concern: ~4 ACL rows per resource over 10⁴–10⁵ resources is a few hundred thousand rows, a few million at 10× growth — a narrow table Postgres handles without effort. **Index design is the concern**, and there is a real gap today.

The list-filter path is the hot one (12+ call sites):

```sql
WHERE idtype.public OR EXISTS (
  SELECT 1 FROM resourceacl
  WHERE mfid = dataset.unique_id AND accessgroup_id = ANY(?) AND role >= ?
)
```

Postgres can plan this two ways. A nested-loop semi-join probes by `mfid` and is served by the existing PK. A **hash semi-join gathers rows by `accessgroup_id` — for which no index exists**, since the PK leads with `mfid`. That plan is a sequential scan of the whole ACL table, and the planner may well choose it for a user in a large project.

Hence:

- **`(accessgroup_id, mfid, role)`** — serves the group→resources direction and the hash plan; the trailing `role` makes it covering, enabling index-only scans.
- **PK `(mfid, accessgroup_id)`** — retained, serves the resource→groups direction.

Two indexes, one per traversal direction of the many-to-many.

Note the apparent contradiction, so it is not "fixed" later: a **standalone** index on a five-value enum is useless, but that same column **trailing** in a composite is a free covering win.

Existing waste to remove: `resourceacl.read` and `resourceacl.write` each carry a standalone index on a two-value column. They go with the columns and must not be replaced by a standalone `permission` index. `idtype.public` has the same problem; a partial index (`ON idtype (mfid) WHERE public`) would be smaller and actually usable.

**The `OR` goes away.** Today's filter is `public = true OR EXISTS(acl…)`, which spans two tables, and such ORs routinely defeat index usage. Once `public` becomes an ACL entry against a virtual group, the whole predicate collapses to `accessgroup_id IN (:my_groups)` — a single indexed clause with no `OR` at all, so the `UNION` rewrite that would otherwise be the tuning lever is not needed.

**Explicitly rejected:** a materialized `user_resource_access` table (adding one user to a 10k-dataset project would mean 10k inserts, plus staleness, to speed a path that is already fast when correctly indexed), and a second cache layer for effective roles (`authorize_user` already TTL-caches group membership at the right level). Revisit only if measurement demands it.

**Unrelated but interacting:** ACL filtering composes badly with deep offset pagination — `?offset=50000` computes and discards 50k access-filtered rows regardless of indexing. Cursor pagination avoids it; steer large-project clients there.

### Auth

#### Introduce a `Principal` type

`authorize_user` currently returns an anonymous 2-tuple, `(orcid_string, list[CachedGroup])`, unpacked by **106 call sites** as `user_unique_id, ag_list = auth_res`. The authenticated caller is a first-class domain concept represented as an unnamed tuple — and it is about to grow roles and two flags. Give it a name:

```python
class GroupMembership(NamedTuple):
    id: int
    group_name: str
    role: Role

@dataclass(frozen=True)
class Principal:
    user_id: str                     # ORCID — remains a plain string
    platform_role: PlatformRole      # 'none' | 'contributor' | 'support' | 'admin'
    is_service_account: bool
    groups: tuple[GroupMembership, ...]

    def role_in(self, group_name: str) -> Role | None: ...
    def groups_at_or_above(self, role: Role) -> list[int]: ...
```

Four properties are deliberate:

- **Named and typed.** `principal: Annotated[Principal, Depends(authorize_user)]` states what a route receives. `Annotated[tuple, ...]` states nothing, and matches FastAPI's own `current_user` convention.
- **Immutable and ORM-free.** A frozen dataclass of plain values, never a `User` instance. `authorize_user` caches its result in a TTL cache and the existing code comment is explicit about why the cached value must be plain, thread-safe, and independent of the closed session. Caching an ORM object would reintroduce exactly the `DetachedInstanceError` class of bug that comment prevents. This extends the pattern `CachedGroup` already established rather than inventing one.
- **Behaviour lives on the type.** `groups_at_or_above(role)` is precisely the computation the `MIN` decomposition needs. Keeping it here stops it being re-derived at each call site, which is how permission bugs get introduced.
- **`user_id` stays a plain string**, so the many places that compare it, write it to `owner_orcid` / `requester_id` / `reviewer_id` columns, and pass it to helpers and background tasks are unaffected.

Consequently the other two signatures take the principal rather than its pieces:

- [ ] `authorize_user` returns `Principal`
- [ ] `require_permission(principal, resource_id, permission)` as the single entry point
- [ ] `read_access_filter_list(model, principal, required_role)`

#### Migrating the 106 call sites

This is a **mechanical** change, and its safety depends on keeping it that way. One identical line per route, no logic touched:

```python
# before
user_unique_id, ag_list = auth_res
# after
user_unique_id, ag_list = auth_res.user_id, auth_res.groups
```

Every downstream use of both variables continues to work untouched, so this lands as a single reviewable commit where every hunk has the same shape. Routes then adopt `principal.platform_role` and `principal.role_in(...)` gradually as they migrate to `require_permission`, deleting the destructuring line as they go.

The distinction that matters: a mechanical 106-site change is safe and reviewable; a semantic one is not. The incremental promise of this migration applies to permission *logic*, not to the type of the auth object — trying to keep the tuple shape forever costs more than changing it once.

#### The `MIN` costs nothing on the read path

Because `min(a,b) >= r ⟺ a >= r AND b >= r`, and per-group roles are already in the principal, "which of my groups clear the bar" is computed in Python and SQL sees only a single column comparison:

```python
qualifying = principal.groups_at_or_above(required)
# SQL: resourceacl.accessgroup_id IN :qualifying AND resourceacl.permission >= :required
```

No join against `useraccessgrouplink` is required in list queries.

### Routes

- [x] Create-on-behalf verification on `POST /datasets` and `POST /samples`: the *effective owner* (named owner, or caller if none given) must hold `>= contributor` in the target project. No project given and naming someone else is rejected outright — there is nothing to verify delegation against. Platform admins bypass, matching every other check
- [x] `GET /users/{orcid}/projects` — instrument-group branch added, `project_id`/`title` only; self and platform admin still get full details
- [ ] Close the unguarded operations listed above — `project_id`/`instrument_name` grammar, collision and registration checks are done (see below); `POST /projects` authorization ("any user, or a registrar role") is still an open decision
- [ ] `POST/PATCH /projects/{proj_id}/users/{orcid}` — `role` param; require `>= editor`, capped at caller's own role
- [x] `GET /projects/{proj_id}/users` — include role
- [x] Move `_is_group_lead`, the project-listing filter, and project member removal off `Project.project_lead_orcid` onto an `owner` standing check; add the FK; make `transfer_ownership` its only write path (column stays permanently — see "Project lead becomes a role")
- [ ] `POST /projects` — create the project's `Resource` row and its single ACL entry (own group at `owner`)
- [ ] `POST /resources/{id}/share`, `DELETE /resources/{id}/share/{orcid}`
- [x] `POST /resources/{mfid}/transfer_ownership` requires `owner` and moves the owner ACL entry and denormalized owner identity atomically. It defaults to a no-op preview, with `?confirm=true` required to execute. Project, dataset, sample, and instrument PATCH schemas reject direct ownership fields.
- [x] `POST /resources/{mfid}/project` — requires `admin` on the resource and `>= contributor` in the target; adds the new project ACL entry and removes the old. Same preview/`?confirm=true` pattern as `transfer_ownership`
- [x] `PATCH /datasets/{dsid}` and `PATCH /samples/{sample_id}` — reject `owner_orcid` and `project_id` with `422` (`extra="forbid"`, same as `ProjectUpdate`), pointing at `transfer_ownership`/`POST /resources/{mfid}/project`
- [ ] **Deferred, tracked separately:** the `instrument_name` change on `PATCH /datasets/{dsid}` must revoke the old instrument group's ACL entry — it currently only adds the new one. Stays on `PATCH` (not a new route) since re-pointing an instrument is an ordinary edit, not an ownership/membership transfer
- [x] Replace global-admin deletion with `require_permission(..., 'delete')`
- [x] `POST /instruments`: allow any authenticated caller, resolve the owner with the shared creation contract, create the owner's `owner` ACL, then create the MFID-named instrument operator group and its `editor` ACL entry. Existing instruments are covered only for operator-group provisioning by `migrations/data/20260810_provision_instrument_operator_groups.py`; descriptive legacy owner values remain case-by-case cleanup.
- [x] `instrument` — add an `instrument_id` handle, validated like `project_id` (no group-namespace collision check needed — groups are keyed by `unique_id`, not `instrument_id`); backfilled via `migrations/data/20260810_backfill_instrument_id.py`, slugified from `instrument_name`, which stays free text
- [x] Validate `project_id` on `POST /projects`: grammar, reserved names, cross-namespace collision — `validate_project_id`/`assert_project_id_available` in `app/utils/project_utils.py`, case-insensitive, checks `AccessGroup` and `Instrument.instrument_name` directly (the latter needed until every instrument has a group — see above)
- [x] Rename existing project access groups to MFIDs — `project_utils.py`, `projects.py`, `resources.py`, `access_groups.py`, `users.py`, `sql_utils.py` now resolve a project's group via `Project.unique_id`, not `project_id`; `migrations/data/20260810_rename_project_groups_to_mfid.py` renames existing groups in place (or creates fresh + flags for manual review on collision, same policy as the instrument version). **Not yet cut over** — this code cannot go live until `Project.unique_id` is backfilled (`20260810_backfill_project_unique_id.py`) and the rename script above has run, in that order; a half-migrated state breaks every project route (see the scripts' own docstrings)
- [x] `AccessGroup.group_type` explicitly distinguishes identity, project, instrument, custom, and system groups. This is narrower than the reverted `owner_orcid`/`resource_mfid` design and has direct authorization readers in generic membership guards and ACL principal serialization. The nullable expansion, audited backfill, and non-null enforcement are revisions `91f3c8a4d2e7` and `c4a7e9d2b615`.
- [x] `POST/DELETE /instruments/{instrument_mfid}/service_accounts/{sa_unique_id}` binds an SA to an instrument at `admin` standing in its operator group. The service-account contributor ceiling prevents that standing from granting edit, deletion, ACL, or transfer authority.
- [x] `POST /datasets` and `PATCH /datasets/{dsid}` — reject unregistered `instrument_name` with `400` instead of creating a group
- [x] `POST /access_groups/{group_name}/join` — reject a non-project group with `400`, so a request cannot be accepted and then fail on approval (already validated against `Project` before this redesign started; 404, not 400, but closes the same gap)
- [x] `GET /instruments` — default `status=active`; `PATCH` status gated to instrument `admin`/`owner`
- [x] `visibility` filter (`all`/`public`/`private`) on dataset and sample listings, replacing `?public=true`
- [x] Expose role in `GET /access_groups/{name}/users`

### Testing

A change touching 70+ authorization call sites currently has **no regression coverage on authorization at all**, and the suite it would land in is already red.

#### Baseline — done

The suite was **28 passed, 22 failed, 6 errors**. It is now **34 passed, 0 failed, 0 errors**. What that took, and what it cost:

| File | Was | Now |
|---|---|---|
| `test_datasets.py`, `test_users.py` | empty — 0 lines | deleted |
| `test_graph_diff.py`, `test_graph_speed.py` | CLI scripts needing a live API; 0 test functions, collected anyway | moved to `scripts/` |
| `test_datasets_endpoints.py` | 21 failed / 1 passed | **deleted** — see below |
| `test_gcs_multipart_endpoint.py` | 6 collection errors | 6 passed |
| `test_username_utils.py` | 19 passed / 1 failed | 20 passed |
| `test_associated_file_remote_backend.py` | 8 passed | 8 passed |

Two of these were worth understanding rather than just fixing.

**The GCS errors were self-inflicted and were hiding a real failure.** `tests/conftest.py` imports `app.api.main` at module scope, registering every SQLModel table before that file's fixture purged `sys.modules` to re-import under patches. The `MetaData` survives the purge, so the re-import raised `Table 'datasetkeywordlink' is already defined`. The purge existed only to patch a module-level `engine = get_engine()`; assigning `gcs_storage.engine` after a normal import does the same job and is what `conftest` already does for other route modules. Underneath the errors sat a genuine failure: `AssociatedFileRead` gained `storage_backend` and `access_note`, which that test's `MagicMock` did not set.

**`test_datasets_endpoints.py` was deleted, not repaired.** It patches `app.api.main.Session`, `app.api.main.sql_create_ds_acl`, and `app.api.main.Path`. Routes moved out of `main.py` long ago, and `sql_create_ds_acl` no longer exists anywhere — it is now `create_standard_acls`. Retargeting the patches to `app.api.routes.datasets` was tried and recovers 1 of 22, so this is a dead API surface rather than a moved module. It had not run since 2025-09.

> **Deliverable, phase 2: replace that coverage.** Deleting 21 tests removes no signal — they were not running — but it does leave the dataset routes uncovered, and that gap must not survive quietly. Rewrite them against the `conftest` fixtures as part of the phase-2 test work, where they belong anyway: the deleted versions asserted on mock call patterns rather than behaviour, which is why they would never have caught the reassignment-never-revokes bug or the uncommitted `POST /resources/{mfid}/access_groups`. Both were found by reading code.

Green is not the finish line, it is the precondition — with a red baseline, a new break is indistinguishable from background noise, so permission tests added to it would buy very little.

#### Keep the decision logic pure

Exhaustive testing is only cheap if the decision does not touch the database. Split the check:

```python
def effective_role(principal, acl_rows) -> Role:                      # pure — no DB, no HTTP
def require_permission(session, principal, resource_id, permission):  # fetches, delegates, raises
```

The combinatorial matrix — five standings × five grants × five operations, plus multi-group, public, platform-admin and no-access cases, roughly 200 assertions — then runs as a parametrized unit test in milliseconds, leaving only a thin fetch-and-raise layer needing integration coverage.

This is the second payoff from `Principal`: a principal is a plain frozen value, so a test constructs one directly, with no database, request, or fixture.

#### What to cover

**Unit, exhaustive and pure.** Every `(standing, grant)` pair resolving to the correct `MIN`; `MAX` across multiple group paths; each operation threshold; platform-admin bypass; `public` granting exactly `viewer` and nothing more; no matching group denied.

**Integration, thin, one per threshold class rather than one per route.** Viewer reads but cannot edit; contributor creates but cannot edit existing values; editor edits but cannot delete; admin deletes; platform contributors receive platform-wide read and create access; service accounts remain capped at contributor unless they are platform administrators. The `auth_as` fixture in `tests/conftest.py` already fakes group membership and is the right foundation.

**Agreement tests, specific to the migration.** For a matrix of `(principal, resource, operation)`, assert the old check and the new check return the same answer, with a deliberate enumerated list of exceptions where behaviour is *supposed* to change — for example project members losing the ability to add other members. This is the test-time counterpart to shadow-mode validation, and it turns "did this break something across 70 routes" into a question with an answer.

### Historical migration design

Do not execute the numbered phases in this section. They describe how the rollout was designed and preserve the reasoning behind expand, cutover, and contract stages. The completed procedure and its dated environment observations are retained in `docs/archive/permission_migration_runbook_2026-08.md`.

~70 call sites use the four existing check helpers (`require_groups` 21, `require_write_access` 16, `require_admin` 12, `read_access_filter_list` 12, `require_read_access` 9). The risk in this project is the migration, not the model.

**Routes are cut over all at once, not one at a time.** Two permission systems coexisting is its own hazard — anyone reading the code has to know which routes are on which check, and that ambiguity lasts as long as the migration does. A single maintainer on a rollback-capable deployment does not need per-route staging; what is needed instead is confidence *before* the cutover, which the expand and validate phases provide.

### Environments and rollout

Work happens against a staging clone of the production database with its own API instance. Two things follow from that, and getting them backwards is the main way this approach yields false confidence.

**Staging proves the migration script. Production shadow mode proves the model.** These are different questions and only one of them a clone can answer:

| Question | Where | Why not the other place |
|---|---|---|
| Does the migration run, backfill correctly, build the index? | Staging | Destructive and iterative; must not touch prod |
| Do the new checks agree with the old ones on real traffic? | **Production**, in shadow mode | Staging has no organic traffic — it would only exercise requests already covered by the agreement tests |

Shadow mode is safe in production by construction: it computes both answers, returns the old one, and logs disagreements. Nothing user-visible changes, which is the entire reason it is built that way. Running it on staging instead would be theatre — it would confirm the cases you thought to try, which is what the agreement tests are for.

**Restore a fresh clone for every migration run.** Iterating the script against an already-migrated staging database tests the wrong thing: a bug gets fixed, the script re-runs, and it passes because the state is already half-converted. Confirm the restore is cheap and scriptable *before* starting — if it is slow and manual, it will get skipped precisely when it matters most.

**Re-run the counting queries against production before the real migration.** The clone is a point-in-time snapshot and drifts from the moment it is taken. Rows created in production in the meantime can introduce fresh public-column disagreements, dangling sample project IDs, or duplicate group owners. The counts are cheap; running them twice is the difference between a migration that fails loudly and one that silently mis-assigns a role.

**Expand, cut over, contract — never all in one migration.** The destructive drops are deferred to a separate phase deliberately. Between expand and contract, a rollback is a code revert and nothing more; once `read`/`write` and the `public` columns are gone, the data the old code reads no longer exists and rollback means restoring from backup under pressure. Carrying dead columns for a few weeks is the price of keeping the scary step reversible.

**Merge the inert work to main early.** Phases 2 and 3 below change no behaviour — `Principal` is a mechanical refactor, and `effective_role` is new code wired to nothing. They can land on main while staging work continues, which takes several thousand lines out of the review that actually matters and leaves one focused cutover PR.

The production runbook supersedes all sequencing in this historical section.

### Phase 0 — baseline

0. ~~**Get the test suite green**~~ — **done.** 34 passed, 0 failed, 0 errors. See Testing → Baseline for what was deleted and the phase-2 deliverable that replaces it.
0.5. **Run the counting queries.** The public-column disagreement count, the dangling `Sample.project_id` count from the archived schema audit, and the `read`/`write` distribution below all fed decisions in phase 1.

### Phase 1 — expand (staging first, then production)

Additive only. Nothing is dropped and no behaviour changes; the old columns stay populated and the old checks stay in force.

1. Add columns and the enums; backfill:
   - every existing project-group membership → `editor`
   - ORCID-group memberships → `owner`
   - project leads → `owner` in their project group (`Project.project_lead_orcid` stays permanently as a denormalized pointer, FK'd, synced only by `transfer_ownership` — see "Project lead becomes a role")
   - a `Resource` row per existing project, plus one ACL entry each: the project's own group at `owner`
   - delete `read_only` ACL rows and `admin`-group ACL rows — **only after the pre-role helpers honour admin-group membership directly**, see below. 721,231 rows on production, 61% of the ACL table
   - remaining ACL rows → `admin` for project groups, `contributor` for instrument groups, `owner` for ORCID groups
   - create the virtual `public` group; insert `(mfid, public_group, viewer)` per public resource, taking `Dataset.public` as authoritative for datasets and `Resource.public` for samples. **Both columns stay** and keep being written until phase 5
   - **add `one_owner_per_group` last**, after every standing is assigned. If it fails to build, the duplicate it reports is pre-existing bad data — most likely a user who was added to someone else's ORCID group, since nothing enforces those being single-member today. Treat a failure as a finding to investigate, not an obstacle to work around by dropping the index.
2. Set `platform_role='admin'` for every existing admin-group member as a temporary cutover measure, including service accounts as described below
   - in the same pass, **remove every non-service-account member from instrument groups**, leaving each group holding only its SA (see Instruments). Confirm first that each affected human is either a platform admin or a member of the projects whose data they need, since this is the one backfill step that removes access people have today
   - create the instrument group for any registered instrument that has none, so group existence stops depending on whether a dataset was ever filed against it
   - add the covering index `(accessgroup_id, mfid, role)`, so the new checks are never measured against an unindexed table

### Phase 2 — inert code (mergeable to main independently)

Changes no behaviour. Safe to merge and deploy while staging validation continues.

3. **Introduce `Principal`** — one mechanical commit rewriting all 106 destructuring lines to `auth_res.user_id, auth_res.groups`. No logic changes, every hunk identical. Land and verify on its own.
4. Add the pure `effective_role` and its exhaustive unit matrix, plus `require_permission` — not yet wired to any route
5. Write the agreement tests: old check vs new check across the matrix, with deliberate exceptions enumerated

### Phase 3 — validate in production

6. **Run shadow mode in production** until mismatches settle to only the enumerated exceptions (see below). This is the gate on the cutover: it is the only step that tests the model against traffic nobody designed a test case for.

### Phase 4 — cut over

Rollback throughout this phase is a code revert. The old columns are still present and still populated.

7. **Cut over.** Replace all check call sites in one deploy, converging inline ACL filters onto the helper and dropping the destructuring lines
8. Add create-on-behalf verification; add the instrument-group branch to the directory route; close unguarded operations
9. Replace `PATCH`-based reassignment with the explicit transfer and project routes; make the instrument-group change revoke the old entry (see Reassignment Never Revokes)
10. Create instrument groups at registration; add the SA binding routes; reject unregistered `instrument_name`
11. Add the `visibility` filter to the dataset and sample listings, replacing `?public=true`

### Phase 5 — contract

Only after the new path has been live long enough to trust. Each of these makes rollback require a restore rather than a revert, which is why they are last and separate.

12. Drop `read`/`write` and their indexes from `resourceacl`
13. Drop `Dataset.public` (and its index) and `Resource.public`

`Project.project_lead_orcid` is not in this list — see "Project lead becomes a role": it stays permanently as a denormalized, FK'd pointer synced by `transfer_ownership`, the same treatment as `Dataset.owner_orcid`/`owner_user_id`.

#### Why the backfill is a uniform assignment, not a mapping

Every ACL row `create_standard_acls` writes is `read=True, write=True`. The sole systematic exception is `read_only`, and those rows are being deleted. The only remaining unknown is manually created entries via `POST /resources/{mfid}/access_groups`, which takes `read` and `write` as caller-supplied parameters — worth one query to confirm the set is empty before relying on this:

```sql
SELECT read, write, count(*) FROM resourceacl GROUP BY read, write;
```

If it returns only `(t,t)` and `(t,f)` — the latter being `read_only` — the backfill needs no conditional logic at all.

#### Deleting the admin ACL rows needs a bypass in the old helpers first

An earlier draft called this deletion safe "because nothing reads them: platform admins bypass the ACL". That is false before cutover. `platform_role` is only consulted by `require_permission`, which no route calls until phase 4 — until then, admin access comes **entirely from those ACL rows**.

On production every admin-group member is a service account, and none belongs to any group but `admin` and its own ORCID group. Deleting 360,726 admin rows in phase 1 would therefore have cut off every instrument writing to Crucible, immediately, while the old checks were still the ones running. Not at cutover, where it would have been expected — at the additive step billed as changing nothing.

The fix is three lines in `auth_utils`: `require_read_access`, `require_write_access` and `read_access_filter_list` short-circuit on admin-group membership, evaluated as a SQL `EXISTS` against `accessgroup` rather than a cached id, so it cannot answer for the wrong database. That grants access to exactly the set that already has it, which makes the rows genuinely dead and the deletion genuinely inert. The short-circuit is deleted along with these helpers at cutover, where `platform_role` takes over.

Two general lessons, both of which cost a real bug here:

- **"Inert" has to be checked against the code that is running, not the code being written.** Both this and the public-group omission came from reasoning about the new model while the old one was still in force.
- **Rows that look like boilerplate can be load-bearing.** 360,726 rows that all say the same thing still constituted somebody's only access path.

#### The admin group is entirely service accounts

Measured on staging: **all 7 admin-group members are service accounts**, and there are no human platform admins at all.

```
admin_developer   bnl_admin   perovskite_synthesis_ui
sa_hip_microscope   sa_m3_microscope   spinbot   titanx
```

This is the historical workaround the plan describes — service accounts were put in the admin group because `GET /users/{orcid}/projects` was gated `admin or be that user`.

**Decision: promote them anyway, and narrow their rights later as separate work.**

An earlier draft promoted humans only, on the grounds that giving service accounts a permanent unconditional bypass defeats goal (2). That is true as an end state and wrong as a migration step. Withholding `platform_role` means the service accounts hold *nothing* the moment cutover switches authorization over — their `admin` standing backfills to `viewer` and the admin group's ACL rows are gone — so every instrument stops writing on the day of the switch. Not breaking ingestion outranks reaching the end state in one move.

So `platform_role='admin'` goes to every admin-group member, service accounts included. This is marginally broader than what they have today: an admin ACL row only exists where one was written, whereas `platform_role` also covers resources with no ACL rows at all. In practice indistinguishable, since `create_standard_acls` writes an admin row on everything.

The cost of this choice, stated plainly: **until the promotion is revoked, a compromised instrument credential reaches every resource on the platform** — which is exactly what goal (2) exists to prevent. It buys a safe cutover, and it has to be paid back.

### Regressing service account rights

After deploying and validating the platform-wide contributor policy:

1. Upgrade service-account clients to create initial metadata with POST and create non-GCS file records atomically.
2. Confirm project discovery, create-on-behalf, uploads, metadata creation, ingestion requests, and resource linking.
3. Set each service account's `platform_role` to `contributor`.
4. Remove each service account from the legacy admin group.

Apply the role changes one service account at a time and monitor ingestion after each change. Instrument binding is no longer a prerequisite for contributor access.

**No human currently holds `platform_role='admin'`.** Worth naming someone, so there is a break-glass path that is not an instrument credential.

#### Existing members become `editor`, not `contributor`

This preserves current behaviour exactly. Today the project group holds `write=True` on every project resource and `require_write_access` passes if *any* of your groups has write — so **any project member can currently edit any project resource**, including colleagues' work. Backfilling to `contributor` would silently remove that from every user on day one.

`editor` is the neutral choice: nobody's effective access changes at cutover. New memberships still default to `contributor` per `POST /projects/{proj_id}/users/{orcid}?role=`, so the stricter model applies going forward, and project admins demote existing members deliberately rather than by migration side effect.

#### Shadow mode

Cutting over in one deploy makes pre-cutover evidence essential. Before step 7 — **in production, against real traffic** — compute both answers on every request, act on the old one, and log where they differ:

```python
def require_write_access(session, ag_ids, mfids):      # still authoritative
    allowed_old = _existing_check(...)
    if SHADOW_MODE:
        try:
            allowed_new = effective_role(principal, acl_rows) >= EDITOR
            if allowed_new != allowed_old:
                logger.warning("permission_shadow_mismatch route=%s mfid=%s user=%s old=%s new=%s",
                               route, mfid, principal.user_id, allowed_old, allowed_new)
        except Exception:
            logger.exception("permission_shadow_error route=%s mfid=%s", route, mfid)
    return allowed_old
```

Three properties keep it safe: the new result is **discarded**, so a bug in it cannot affect any request; it is wrapped in **its own try/except**, so unfinished code cannot take down a working route; and it is **behind a flag**, so it can be switched off instantly.

It costs almost nothing, because `effective_role` is pure and the ACL rows have already been fetched by the old check — one in-memory call, no extra query. That is only true given the pure/impure split described in Testing; the two decisions depend on each other.

Mismatches are the deliverable:

| Mismatch | Meaning |
|---|---|
| new denies, old allowed | An intended tightening, or the backfill under-assigned a role |
| new allows, old denied | Almost always a bug — investigate every one |
| new raises | An implementation gap, surfaced without a user seeing a 500 |

Anything not on the enumerated exception list is a finding. Two practical cautions: **rate-limit or sample the logs**, since a badly wrong backfill could make every request emit; and **count matches too, per route**, otherwise "no mismatches" is indistinguishable from "this route was never exercised."

That last caution is also why this runs in production rather than on the staging clone. Per-route match counts are the exit criterion — the cutover is gated on every route having been exercised enough to mean something, and a staging instance with no organic traffic cannot produce that evidence no matter how long it runs.

Shadow mode and the agreement tests are complementary — the tests prove the *model* is right against cases you thought of, shadow mode proves your *data* matches what the model assumes, against traffic you did not.

---

## Future Work

Deferred with reasons, so the decisions are not relitigated from scratch.

### Multi-project resources

Worth being precise about what is and is not missing, because the two halves have very different costs.

**Access across projects already works.** The ACL layer was never single-project — a resource can carry a row per project group, each with its own grant, and `MAX` over groups already resolves someone who belongs to several of them:

```
resourceacl:
  (D, bob_orcid_group,  owner)
  (D, 10k_perovskites,  admin)      <- primary
  (D, other_project,    viewer)     <- shared in, capped at read
```

**Nothing in the role model needs to change** when multi-project lands. That is the expensive part, and it is already done.

**Listing across projects is the gap.** `Dataset.project_id` is a single column and drives `?project_id=`. With the ACL above, a member of `other_project` *can read* D — it passes the auth filter and appears in a plain `GET /datasets` — but `GET /datasets?project_id=other_project` will not return it. Cross-project sharing works; the shared-in resource is just invisible in the recipient project's own view.

The fix is a `resource_project_link` table, with one design rule: **it records belonging, not access.** Linking a resource to a project writes both a link row and an ACL row. If the link table replaced project ACL rows instead, the permission check would need two code paths — link table for projects, `resourceacl` for users and instruments — and sharing with a project would stop being the same operation as sharing with a person.

Deferred because of client breakage, not model complexity: `project_id` appears in `DatasetCreate`/`Read`/`Update`, the sample equivalents, and client-side `?project_id=` filters. Either `Dataset.project_id` survives as a denormalised "primary" for compatibility, or every client changes.

On performance, the join is roughly a wash rather than a regression, provided the index is right. Because `resource_project_link.resource_mfid` *is* the value listings order by, an index on `(project_id, resource_mfid)` yields project-filtered rows already in output order from a two-column table; take the page, then join to `dataset` by primary key. Which side wins depends on selectivity. The archived schema audit records the indexes observed at the time.

### Instrument ↔ project association

`POST/DELETE /instruments/{id}/projects/{proj_id}`, granting the instrument group `contributor` on the project resource. This remains deferred. Service-account create-on-behalf is temporarily allowed without project authorization to preserve ingestion, so the API does not yet prove that an instrument is approved for the target project. Implement this association before removing that transitional exception or granting instruments scoped access to existing project resources.

### Enforcing service-account instrument bindings

Dataset creation currently verifies that the selected instrument is registered but does not require a service-account caller to belong to that instrument's operator group. Enforcing the binding immediately would break credentials that have not yet been properly associated with their instruments, so this remains deferred until operators complete that setup.

When an unbound service account successfully creates a dataset for a registered instrument, the API logs a warning containing the service-account identity, dataset MFID, instrument slug, and instrument MFID. The request remains successful and the response contract is unchanged. Once production warnings show that active service accounts are properly bound, replace the warning with a 403 for unbound service accounts while retaining the platform-admin bypass policy chosen at cutover.

### Deriving the instrument grant instead of storing it

**Superseded.** This entry argued for computing the instrument grant from
`Dataset.instrument_id` at check time rather than storing an ACL row, and
rejected it on the grounds that it "breaks the invariant that the ACL table is
the single source of truth".

That reasoning was backwards. It assumed the grant means *"belongs to
instrument X"*, in which case the stored row is a cached projection of the
instrument column and the two can drift. Under the corrected reading — the
grant records **who produced the dataset**, fixed at creation (see Instruments)
— there is nothing to derive and nothing to drift. The row is authored state,
not a copy of anything.

### Delegated authority beyond creation

Service accounts use `platform_role=contributor` rather than impersonating a named user. `accessible_to_user` narrows collection results, while the `owner` field identifies the registered person for create-on-behalf validation and ownership.

### Object classification (tags)

`public` is currently the only classification a resource carries, as a boolean. Real scientific-data needs — `sensitive`, `embargoed`, `proprietary` — are not expressible. The natural generalization is a tag set per resource, with policy mapping (role, tag) to entitlements, and **tag-application authority** as the control: who may declassify a resource by marking it public is exactly the question the current ungated `public` field fails to answer. `public` is the degenerate first case of this.

### Project privacy / sharing policy

`POST /resources/{id}/share` is discretionary — anyone with `admin` on a resource may share it outside the project at will, and no project-level policy can prevent it. Since the primary project is granted `admin`, that includes every project admin. For confidential or industry-partnered work that may be wrong.

The proposal is a per-project setting governing outward exposure. **The critical implementation constraint is that it must be enforced at grant time, not evaluation time.** A policy that computes an effective role and then subtracts from it is a deny rule: it breaks the monotonicity invariant and makes access unauditable. Instead, the policy refuses to *create* the offending ACL entry. `resourceacl` remains the single source of truth and `effective_role` stays a pure `MAX(MIN(...))`.

Three levels, ordered by outward exposure:

| Level | Publish (ACL entry for `public`) | Share outside the project |
|---|---|---|
| `open` | anyone with `admin` on the resource | anyone with `admin` on the resource |
| `restricted` | project admin only | project admin only |
| `closed` | forbidden | project admin only |

Once `public` is an ACL entry against a virtual group rather than a boolean, these two columns stop being separate mechanisms. Both are the same question — *which groups may appear in this resource's ACL, and who may put them there* — so the policy is one rule applied at ACL-write time, with `public` simply being one of the groups it can forbid. That is a good deal simpler to implement than gating a boolean field and a share route independently.

Enforcement points: `POST /resources/{id}/share`, setting `public` on resource create/update, and adding an ACL entry for a different project's group.

Two hazards:

- **The default must be `open`.** Publishing is currently ungated, so defaulting existing projects to anything stricter would break live workflows. Stricter levels are opt-in per project.
- **For a resource in multiple projects, most restrictive wins** — consistent with the `MIN` philosophy elsewhere in the model.

Resources with no project have no policy to apply; the owner decides, since it is personal data by definition.

This should also **subsume the per-project `discoverable` flag** deferred during the project-discovery work. Discoverability and sharing policy are the same concern — how exposed is this project's existence and data — and should not ship as two parallel flags.

Naming is unresolved. "Private" is ambiguous (private from whom?), which is why the levels above describe policy rather than state. Worth settling before implementation.

### Ownership transfer — policy questions

The mechanism is built: `POST /resources/{mfid}/transfer_ownership`, with a `?confirm=true` preview/execute split (see Reassignment) since a completed transfer can't be self-reversed by the person who made it.

**Decided: what happens to the previous owner differs by resource shape, not one uniform rule.** A project demotes the previous lead to `admin` — they're still a real member of the same shared group, just no longer in charge. A dataset/sample/instrument removes the previous owner's ACL entry outright, because that grant lives on their *personal* ORCID group — leaving "Alice's personal group is admin here" behind after she no longer owns it doesn't fit what a personal group represents. Whatever access she keeps afterward comes from an actual standing elsewhere (project membership, a share), not a leftover grant tied to her identity group.

Still open: whether a platform admin may transfer on an unresponsive owner's behalf when someone leaves the lab, and whether ownership can rest with a group rather than an individual, which would matter for long-lived instrument or facility data. Neither blocks the route — they change its authorization branch and can be added without reshaping it.

### Authorizing on-behalf-of at shared machines

Service accounts already avoid researchers typing API keys into lab computers, but the SA's assertion that it is acting for a given researcher is currently just trusted. The standard solution is the OAuth 2.0 Device Authorization Grant (RFC 8628), designed for exactly this input-constrained shared-device case: the researcher approves the session from their own phone, and the instrument receives a short-lived token scoped to that user. Worth considering if the trust placed in instrument software needs hardening.

### Permission-change auditing

Platform-admin bypass and ACL modifications are currently indistinguishable from ordinary authorized actions in any audit trail. A `ResourceAuditLog` table — one row per mutation with actor, action, and target — would cover this and the broader "who changed what" gap that `mf-record-prod` snapshots only partially address.

### Project identity: mfid vs `project_id`

> **Done.** MFID-named groups shipped (Group C). `Dataset.project_id`/`Sample.project_id` and `Dataset`'s instrument reference resolve through the mfid — added as parallel `project_mfid`/`instrument_mfid` columns rather than repointing the existing ones, so nothing broke mid-migration. The project's own `idtype.mfid`/`ResourceACL.mfid` also uses `unique_id`, not `project_id`, closing the one identity reference that was still label-keyed. `project_id` itself is now renamable via `PATCH /projects/{proj_id}` — safe because every read path that surfaces a dataset/sample's `project_id` now batch-resolves it live from `project_mfid` via `resolve_project_mfids_to_ids`, rather than trusting the text stored at creation time. What remains deferred: dropping the old `project_id`/`instrument_name`-as-FK columns once the new path has been live long enough to trust (see below).

`project_id` did two jobs at once — the stable identity every foreign key pointed at, *and* the human-readable label in URLs and UI. Those pull in opposite directions the moment a project needs to be renamed. The model already solved this correctly one table over: `User` separates `unique_id` (ORCID, stable, what FKs reference) from `username` (mutable, unique, what people type). Projects and instruments now follow the same split for their *data* foreign keys.

**What actually moved (this session):**

| Reference | Now |
|---|---|
| `Dataset.project_mfid`, `Sample.project_mfid` (new) | FK → `project.unique_id`, populated at create/reassign time |
| `Dataset.instrument_mfid` (new) | FK → `instrument.unique_id`, populated at create/update time |
| `Dataset.project_id`, `Sample.project_id` (old) | **kept**, still written in parallel — still what list/search/graph queries filter and display on |
| `Dataset.instrument_id` (int, internal), `instrument_name` (free text) | **unchanged** — `instrument_mfid` is a new column alongside, not a replacement |
| API surface (`DatasetCreate`/`Read`, `SampleCreate`/`Read`) | unchanged for `project_id`; `Dataset` gains an optional `instrument_id` (the validated slug) as an alternative to `instrument_name`, resolved server-side either way |
| `ResourceACL` | unaffected — already keyed on `accessgroup.id` (int), not a label |

Resolution helpers: `resolve_project_id_to_mfid`/`resolve_project_mfids_to_ids` (`app/utils/project_utils.py`), `resolve_instrument_id_to_mfid`/`resolve_instrument_mfids_to_ids` (`app/utils/instrument_utils.py`) — single lookup for writes, batched for anything returning multiple rows (never one query per row).

**Also fixed, same pass:** `create_project` used to seed `Resource.mfid`/`ResourceACL.mfid` for a project's own idtype/ACL row with `project_id` — the same label-as-identity bug, just pointed at itself. Every `require_permission`/`require_grant` call site and generic `/resources/{mfid}/...` route with a project special case (`transfer_ownership`, `reassign_resource_project`, `get_resource`, `_group_kind`/`_resolve_grantee`) now uses `Project.unique_id`. Existing projects repointed via `migrations/data/20260813_repoint_project_resource_identity.py` (idtype + resourceacl rows; `scientific_metadata_new`/`deletionrequest` confirmed to hold zero project rows on staging, so needed no handling). Along the way, found and fixed `AccessGroupJoinRequest.group_name`'s FK — it pointed at `accessgroup.group_name` (a project's MFID since C4b) but every route wrote/read it as a `project_id`, live-broken since C4b with no test to catch it. Switched to `accessgroup_id: int`, matching this repo's general preference for an internal-id FK over a value-based one wherever the relationship is purely internal and single-target (see the runbook's new section for both).

**Rename, shipped:** `ProjectUpdate` gained an optional `project_id` field, validated the same way as at creation (`validate_project_id` + `assert_project_id_available` + a self-excluding uniqueness check against `Project`). No dedicated rename route or MFID-based routing was needed — `PATCH /projects/{proj_id}` already resolves the target project before touching any field, so renaming is just another field edit. `Instrument.instrument_id`/`instrument_name` needed no equivalent work; both were already safely renamable (confirmed by reading `update_instrument`, not just assumed).

Every read path that surfaces a dataset or sample's `project_id` — `GET/PATCH /datasets/{id}`, `GET /datasets`, `GET /datasets/search`, the equivalent three for samples, `GET /resources/{mfid}` for both, and the lineage listings (`/datasets/{id}/parents`, `/children`, `/datasets/{id}/samples`, `/samples/{id}/parents`, `/children`) — now batch-resolves `project_mfid → project_id` via `resolve_project_mfids_to_ids` before serializing, instead of echoing the (possibly stale) text column. The `?project_id=` query filters on `/datasets/search`, `/samples/search`, and the project/entity graph endpoints (`projects.py`, `entities.py`) resolve the incoming label to `project_mfid` once and filter on that, so querying by a project's *current* name finds everything in it, including entities created before the rename.

The two dataset lineage endpoints (`/parents`, `/children`) turned out not to be just a staleness gap: they returned raw `Dataset` rows through `DatasetRead`, which has the same `instrument_id: str` vs. the ORM's legacy int column mismatch as the production crash fixed earlier — any parent/child dataset with a registered instrument would 500. Fixed the same way, with regression coverage in `tests/test_lineage_listing_resolution.py`.

**Still deferred:**

- **`UserProjectLink`.** Confirmed dead (zero reads, already marked for a "phase-5 drop" in its own docstring) but left alone — dropping a table is destructive and unrelated to this migration's actual ask.
- **Dropping the old `project_id`/`instrument_name`-as-FK columns.** Expand-only for now, matching this repo's own migration discipline (see the runbook's ordering rules) — the drop is a later, separate "contract" step once the new columns have been live long enough to trust.
- **Generalizing this pattern onto `idtype` itself** (a shared `human_readable_id` column, unifying the project/instrument resolution helpers into one and closing the gap where a new `instrument_id` and an existing `project_id` could theoretically collide with each other) — raised, has real merit, deliberately out of scope here. Would mean either duplicating `project_id`/`instrument_id` onto a second table or moving them off `Project`/`Instrument` entirely.
