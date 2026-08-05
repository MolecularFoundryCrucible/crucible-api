# Permission System Redesign

## Goals

The current permission model is binary: read or write access to a group's resources. This is too coarse for a platform where researchers, collaborators, facility staff, instrument software, and operators all have different levels of responsibility.

The redesign adds five roles (`viewer`, `contributor`, `editor`, `admin`, `owner`). A role appears in two places — your standing in a group, and what that group was granted on a resource — and your effective rights are the weaker of the two. Platform admin access is a separate bypass outside the ACL system entirely.

Two goals drive the specifics:

1. **Different roles within a project.** A project viewer and a project admin should not have identical access to the project's data.
2. **Instrument service accounts that reach only what they need.** An instrument must be able to create datasets on behalf of researchers without being able to read every dataset on the platform.

---

## Core Concepts

### Resources

A resource is anything with an MFID: datasets, samples, and instruments. Every resource has ACL entries in `resourceacl` naming which groups may access it and at what role.

> **Note on projects.** The earlier draft of this document treated projects as resources with rows in `idtype`. They are not — `create_project()` does not create a `Resource` row, so every project's `creation_time`/`modification_time` is currently null. Making projects first-class resources is deferred (see Future Work); nothing in this phase depends on it.

**[FR]**: It is time that projects are also added to the resource table, this should simplify some of the logic and allow to have their fields properly filled.

### ACL entries at resource creation

What a newly created dataset or sample actually gets. Created by U, owned by O, in project P, on instrument I:

| Group | Today | Proposed | Applies to |
|---|---|---|---|
| O's ORCID group | read+write | `owner` | both |
| P's project group | read+write | `admin` | both, when `project_id` given |
| I's instrument group | read+write | `contributor` | **datasets only**, when `instrument_name` given |
| U's ORCID group, if U≠O | read+write | **dropped** | — |
| `admin` | read+write | **dropped** — bypass | — |
| `read_only` | read-only | **dropped** — bypass | — |

Roughly six rows becomes three. Three of those changes carry reasoning worth keeping:

**The ACL grant is a ceiling, not an assignment.** Because effective role is `MIN(standing, grant)`, the value here caps what the group's members can reach. The primary project is granted `admin` so that project roles apply at face value — granting `editor` instead would silently demote project admins on their own project's data, which is its only effect. The same column expresses sharing intent elsewhere: a project a resource is shared *into* gets `viewer` or `editor`, a real cap.

Note what `admin` on the primary project unlocks beyond deletion review: project admins can delete member resources directly (both paths write a `DeletionAuditLog` row) and can manage ACL entries, which includes sharing project data outward. That last point is what the deferred project-privacy setting addresses.

**The instrument group gets `contributor`, not `editor`.** An instrument service account can create and manage its own output but cannot modify another researcher's datasets taken on the same machine.

**The requestor entry is dropped.** It was redundant once create-on-behalf must name a project — the creator reaches the resource through that project group. Keeping it meant a service account accumulating a personal ACL entry on every dataset it ever created, thousands of rows expressing nothing. "Who created this" is audit-log information, not an access grant.

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

`role` lives on **both** `useraccessgrouplink` (your standing in a group) and `resourceacl` (what that group was granted on that resource).

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

**[FR]**: this is crucial that we do properly.

### Permission thresholds

| Operation | Requires |
|---|---|
| read | `>= viewer` |
| create in a project | `>= contributor` in the project group |
| edit an existing resource | `>= editor` |
| delete | `>= admin` |
| manage ACL entries | `>= admin`, and only at or below your own role |
| transfer ownership | `owner` |

### Design invariants

Two properties that must hold, stated so they are not accidentally broken later:

- **No deny rules.** The model is monotonic — adding a grant can never reduce anyone's access. Deny rules are where ACL systems stop being auditable.
- **Bypass access is never materialized.** Platform-wide access lives in the check function, never as per-resource ACL rows (see Platform Access).

### Model rationale

The two-axis design is a deliberate single level of indirection between "who the user is" and "what the object is." The alternative — one axis — forces a group per (object-type × permission-level) combination, the classic `report-readers` / `report-writers` proliferation. Collapsing this back to one axis to "simplify" it would reintroduce that, and would break sharing as described above.

### Groups

| Group | Named after | Members |
|---|---|---|
| Project group | `project_id` | Project members, at their project role |
| ORCID group | user's ORCID | The user, always at `owner` |
| `admin` group | `admin` | Platform admins (organizational only — see below) |
| Instrument group | instrument name (today) / MFID (target) | Instrument service account, facility staff |

> **Migration note.** Instrument access groups are currently named after `instrument_name.lower()` (`datasets.py:195`), not the instrument MFID. Renaming them to MFIDs is a data migration touching existing ACL rows, not a greenfield choice. Either keep the name-based convention or schedule the rename explicitly.

---

## Platform Access

### Platform admins

Platform admins bypass all resource-level checks. This is a flag on `User`, not group membership — groups control resource access, user flags control system-level bypass.

```sql
ALTER TABLE "user" ADD COLUMN is_platform_admin BOOLEAN DEFAULT FALSE;
```

`authorize_user` returns the `User` object alongside the group list, so the check needs no extra query:

```python
def require_permission(user, resource_id, user_groups, permission='read'):
    if user.is_platform_admin:
        return True
    # effective-role check against resourceacl
```

**Stop materializing bypass access.** `create_standard_acls` currently writes an ACL row for the `admin` group and the `read_only` group on every resource created. Under this model the `admin` row is dead weight — platform admins never consult the ACL. If `read_only` is a platform-wide auditor group it should become a bypass too. Removing both drops per-resource ACL rows from ~6 to ~4 and eliminates two inserts per resource creation.

The `admin` group remains as an organizational container and for route-level checks, but no longer drives the bypass.

### Service accounts

`is_service_account` is a **label only**. It grants no bypass of any kind — not read, not routes.

This is a deliberate reversal of the earlier draft, which proposed that any service account could read every resource on the platform without an ACL entry. That directly contradicts goal (2): a single compromised instrument credential would expose every dataset. Neither behaviour was ever implemented, so there is nothing to unwind.

An instrument service account reaches what it needs through three scoped mechanisms:

**1. Directory lookup.** To create a dataset for a researcher, the instrument must resolve who they are and which projects they belong to. `POST /users/resolve` and `GET /users/by-username/{username}` are already open to all authenticated users and return `UserPublicRead` (no email).

The gap is `GET /users/{orcid}/projects`, gated `require_groups(ag_list, {'admin', orcid})` — admin **or be that user**. A service account is neither. **This single route is why service accounts were historically put in the admin group.** Relax it to any authenticated caller returning project id and title only — not the lead's email, not the member list. Project membership is directory metadata, not research data.

**2. Create-on-behalf.** When a caller creates a resource naming someone else as owner, the server verifies that the *named user* may create in the target project:

```
caller creates resource with owner_orcid = X, project_id = P
if X != caller:
    require X is a member of P at >= contributor
owner ACL entry -> X's ORCID group at `owner`
```

Authority derives from the researcher, not from the service account, so **the SA needs no standing in P at all.** This is real delegation rather than impersonation-by-assertion.

This also closes a hole that exists today for *every* caller, not only service accounts: `owner_orcid` arrives in the request body and is used unverified (`datasets.py:156-160`). Any authenticated user can currently create a dataset attributed to anyone, in any project.

**3. Instrument metadata.** The SA holds `admin` in its instrument group, letting it update the instrument it runs on and nothing else.

Service accounts can never manage ACL entries, regardless of role. Enforced in code.

> **Consequence of deferring instrument↔project association.** Without it, an SA has no read access to project data. Create-on-behalf works, but "link this new dataset to a sample the researcher made last week" fails — that touches a resource the SA has no standing on. If that workflow matters, the extension is to let an SA act with the target user's permissions *capped at `contributor`*: it can do what X could do, but never delete or manage ACLs. Left out until the workflow actually bites.

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

> **Existing behaviour this changes.** `POST /projects/{proj_id}/users/{orcid}` is currently gated `require_groups(ag_list, {'admin', proj_id})`, which means **any member of a project can add anyone else to that project** — a viewer can add a collaborator. Under roles this becomes project `admin` only.

### "Project lead" becomes a role

Several routes currently key off `Project.project_lead_orcid` directly: join-request review (`_is_group_lead` in `access_groups.py`), project member removal, and project listing. Under this model "lead" becomes "holds `admin` in the project group." Those checks must be migrated together, or the two notions will drift.

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

An instrument is a resource with an MFID, an owner, and ACL entries, plus an instrument group controlling who may manage it:

| Role | Who | Rights |
|---|---|---|
| `owner` | Registrar | Full management, decommission, transfer |
| `admin` | Facility staff | Edit metadata, change status |
| `admin` | Instrument service account | Update instrument metadata programmatically |

The service account holds `admin`, not `owner` — it cannot decommission the instrument or manage group membership.

### Status lifecycle

Instruments are not deleted; a `status` column tracks lifecycle (`active`, `maintenance`, `decommissioned`). `GET /instruments` defaults to `status=active`. Only instrument `owner`/`admin` or platform admins may change status.

### Dataset ownership

Datasets created by an instrument service account belong to the researcher, not the instrument. The `owner_orcid` in the payload names the researcher and the `owner` ACL entry is created for them — subject to the create-on-behalf verification above.

---

## Currently Unguarded Operations

Discovered while mapping existing routes. These take `auth_res` — so authentication is required — but perform **no authorization check at all**. They must be closed as part of this work rather than found one at a time:

| Route | Today | Should require |
|---|---|---|
| `POST /projects` | none | decide: any user, or a registrar role |
| `POST /instruments` | none | platform admin or instrument registrar |
| `PATCH /instruments/{instrument_unique_id}` | none | instrument `admin`/`owner` |
| `POST /datasets`, `POST /samples` | none | `>= contributor` in the target project, plus create-on-behalf verification |
| setting `public = true` | none | `>= editor`, arguably project `admin` |
| `instrument_name` on `POST /datasets` | none | validate against the `Instrument` table |

The instrument ones are the most exposed: nothing currently prevents any authenticated user from renaming or re-pointing someone else's instrument.

**Publishing is the subtlest.** `public` is a plain pass-through field (`ri.public = ds.public`, `datasets.py:185` and `samples.py:198`) with no gate, so any user who can create or edit a resource can make it world-readable. For projects doing confidential or industry-partnered work, a contributor can currently publish unilaterally.

**Unvalidated `instrument_name` creates access groups.** `create_dataset` looks the name up in the `Instrument` table (`datasets.py:164`) and sets `instrument_id = None` when there is no match — but then creates an access group from the raw string anyway (`datasets.py:197-201`) and writes an ACL entry for it. So `POST /datasets {"instrument_name": "xrd-typo"}` silently produces a permanent access group named `xrd-typo`. A typo pollutes the group table and attaches a meaningless ACL entry. The name should be validated against registered instruments.

`POST /projects` deserves a deliberate exception to this list. It has no check today, and that is arguably correct: ad-hoc single-member projects are the intended way to group personal work (see Future Work on personal projects). Requiring approval to create one would kill that workflow and push people back toward wanting automatic personal projects. Leave it open, knowingly.

Several other routes show no helper call but do filter inline on `ResourceACL.accessgroup_id.in_(ag_ids)` (e.g. `add_keyword`). Those are checked, just not through the shared helper — they need converging on `require_permission`, not fixing.

---

## Implementation

### Schema

- [ ] `CREATE TYPE resource_role AS ENUM ('viewer','contributor','editor','admin','owner')`
- [ ] `useraccessgrouplink` — add `role resource_role NOT NULL DEFAULT 'contributor'`
- [ ] `resourceacl` — add `role resource_role`
- [ ] `resourceacl` — drop `read`/`write` **and their indexes** after migration
- [ ] `resourceacl` — add covering index `(accessgroup_id, mfid, role)`
- [ ] `user` — add `is_platform_admin BOOLEAN DEFAULT FALSE`
- [ ] `instrument` — add `status VARCHAR DEFAULT 'active'`

### Indexing

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

Existing waste to remove: `resourceacl.read` and `resourceacl.write` each carry a standalone index on a two-value column — no selectivity, pure write cost. They go with the columns and must not be replaced by a standalone `role` index. `idtype.public` has the same problem; a partial index (`ON idtype (mfid) WHERE public`) would be smaller and actually usable.

**Designated tuning lever.** The `public = true OR EXISTS(...)` filter spans two tables, and such ORs routinely defeat index usage. If list latency regresses, the first fix is rewriting it as a `UNION` of two well-indexed queries — the public set and the ACL-accessible set — not adding more indexes.

**Explicitly rejected:** a materialized `user_resource_access` table (adding one user to a 10k-dataset project would mean 10k inserts, plus staleness, to speed a path that is already fast when correctly indexed), and a second cache layer for effective roles (`authorize_user` already TTL-caches group membership at the right level). Revisit only if measurement demands it.

**Unrelated but interacting:** ACL filtering composes badly with deep offset pagination — `?offset=50000` computes and discards 50k access-filtered rows regardless of indexing. Cursor pagination avoids it; steer large-project clients there.

### Auth

- [ ] `authorize_user` returns `(User, list[AccessGroup])` with roles attached
- [ ] `require_permission(user, resource_id, user_groups, permission)` as the single entry point
- [ ] `read_access_filter_list(model, ag_ids, required_role)` — gains the role argument

The `MIN` costs nothing on the read path. Because `min(a,b) >= r ⟺ a >= r AND b >= r`, and per-group roles are already in the auth context, "which of my groups clear the bar" is computed in Python and SQL sees only a single column comparison:

```python
qualifying = [g.id for g in user_groups if g.role >= required]
# SQL: resourceacl.accessgroup_id IN :qualifying AND resourceacl.role >= :required
```

No join against `useraccessgrouplink` is required in list queries.

### Routes

- [ ] Create-on-behalf verification on `POST /datasets` and `POST /samples`
- [ ] Relax `GET /users/{orcid}/projects` to any authenticated caller, public-safe fields
- [ ] Close the unguarded operations listed above
- [ ] `POST/PATCH /projects/{proj_id}/users/{orcid}` — `role` param; require project `admin`
- [ ] `GET /projects/{proj_id}/users` — include role
- [ ] Migrate `_is_group_lead` and other `project_lead_orcid` checks to the `admin` role
- [ ] `POST /resources/{id}/share`, `DELETE /resources/{id}/share/{orcid}`
- [ ] Replace global-admin deletion with `require_permission(..., 'delete')`
- [ ] `GET /instruments` — default `status=active`; `PATCH` status gated to instrument `admin`/`owner`
- [ ] Expose role in `GET /access_groups/{name}/users`

### Migration path

~70 call sites use the four existing check helpers (`require_groups` 21, `require_write_access` 16, `require_admin` 12, `read_access_filter_list` 12, `require_read_access` 9). The risk in this project is the migration, not the model.

1. Add columns and the enum; backfill roles from current booleans:
   - `read=true, write=false` → `viewer`
   - `read=true, write=true` → `contributor`
   - ORCID-group memberships → `owner`
   - Project leads → `admin`
2. Set `is_platform_admin=true` for existing platform admins
3. Add `require_permission` alongside existing checks — no routes rewired yet
4. Migrate routes one at a time, converging inline ACL filters onto the helper
5. Add create-on-behalf verification; relax the directory route; close unguarded operations
6. Add the covering index; drop `read`/`write` and their indexes

Existing `write=true` users map to `contributor`, not `editor` — no silent privilege grants. Project leads promote individuals deliberately.

---

## Future Work

Deferred with reasons, so the decisions are not relitigated from scratch.

### Multi-project resources and personal projects

The earlier draft proposed a `resource_project_link` table, a personal project per user, and eventually dropping `project_id` from `Dataset` and `Sample`. Deferred: it is the largest and most client-breaking piece — `project_id` appears in `DatasetCreate`/`Read`/`Update`, the sample equivalents, and client-side `?project_id=` filters — and landing it alongside a 70-call-site permission migration means two high-risk changes at once.

Personal projects may also be unnecessary. Their stated purpose was "your workspace always reflects everything you created," but the owner ACL entry already provides that: *resources where my ORCID group holds `owner`* is a query that needs no new table, no `is_personal` column, and no per-user backfill.

### Instrument ↔ project association

`POST/DELETE /instruments/{id}/projects/{proj_id}`, auto-granting the service account `contributor` in the associated project group. Deferred as too much operational machinery for now; create-on-behalf works without it. Revisit when scoped *read* access for instruments is needed (see the consequence noted above), or when it becomes desirable to state explicitly which instruments a project has approved.

### Delegated authority beyond creation

Letting a service account act with a named user's permissions capped at `contributor`, so it can operate on that user's existing resources (e.g. linking a new dataset to a pre-existing sample) without gaining delete or ACL rights. Only needed if the linking workflow proves necessary.

### Object classification (tags)

`public` is currently the only classification a resource carries, as a boolean. Real scientific-data needs — `sensitive`, `embargoed`, `proprietary` — are not expressible. The natural generalization is a tag set per resource, with policy mapping (role, tag) to entitlements, and **tag-application authority** as the control: who may declassify a resource by marking it public is exactly the question the current ungated `public` field fails to answer. `public` is the degenerate first case of this.

### Project privacy / sharing policy

`POST /resources/{id}/share` is discretionary — anyone with `admin` on a resource may share it outside the project at will, and no project-level policy can prevent it. Since the primary project is granted `admin`, that includes every project admin. For confidential or industry-partnered work that may be wrong.

The proposal is a per-project setting governing outward exposure. **The critical implementation constraint is that it must be enforced at grant time, not evaluation time.** A policy that computes an effective role and then subtracts from it is a deny rule: it breaks the monotonicity invariant and makes access unauditable. Instead, the policy refuses to *create* the offending ACL entry. `resourceacl` remains the single source of truth and `effective_role` stays a pure `MAX(MIN(...))`.

Three levels, ordered by outward exposure:

| Level | `public = true` | Share outside the project |
|---|---|---|
| `open` | anyone with `admin` on the resource | anyone with `admin` on the resource |
| `restricted` | project admin only | project admin only |
| `closed` | forbidden | project admin only |

Enforcement points: `POST /resources/{id}/share`, setting `public` on resource create/update, and adding an ACL entry for a different project's group.

Two hazards:

- **The default must be `open`.** Publishing is currently ungated, so defaulting existing projects to anything stricter would break live workflows. Stricter levels are opt-in per project.
- **For a resource in multiple projects, most restrictive wins** — consistent with the `MIN` philosophy elsewhere in the model.

Resources with no project have no policy to apply; the owner decides, since it is personal data by definition.

This should also **subsume the per-project `discoverable` flag** deferred during the project-discovery work. Discoverability and sharing policy are the same concern — how exposed is this project's existence and data — and should not ship as two parallel flags.

Naming is unresolved. "Private" is ambiguous (private from whom?), which is why the levels above describe policy rather than state. Worth settling before implementation.

### Ownership transfer

When an owner leaves, their resources remain under their ORCID group, and only they can modify that ACL entry. Open questions: who may initiate a transfer (platform admins only, or project admins too); whether the previous owner retains a lower role; whether ownership can transfer to a group rather than an individual.

### Projects as first-class resources

`create_project()` does not create a `Resource` row, so projects have no MFID entry, no `creation_time`/`modification_time`, and cannot carry ACL entries or scientific metadata the way datasets and samples do. Fixing this touches ACL and deletion-filter assumptions that key off `Resource` rows existing.

### Authorizing on-behalf-of at shared machines

Service accounts already avoid researchers typing API keys into lab computers, but the SA's assertion that it is acting for a given researcher is currently just trusted. The standard solution is the OAuth 2.0 Device Authorization Grant (RFC 8628), designed for exactly this input-constrained shared-device case: the researcher approves the session from their own phone, and the instrument receives a short-lived token scoped to that user. Worth considering if the trust placed in instrument software needs hardening.

### Permission-change auditing

Platform-admin bypass and ACL modifications are currently indistinguishable from ordinary authorized actions in any audit trail. A `ResourceAuditLog` table — one row per mutation with actor, action, and target — would cover this and the broader "who changed what" gap that `mf-record-prod` snapshots only partially address.

---

## Schema Hygiene and FK Conventions

Found while auditing the permission path. Deliberately **not** part of the permission redesign — this is schema cleanup with its own migration risk, and bundling it would make an already-large change harder to review. Recorded here because item 1 directly affects a query the permission model depends on.

### 1. `owner_orcid` / `owner_user_id` duplication

`Dataset` and `Sample` each carry **both** an owner reference:

```python
owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
owner_orcid:   Optional[str] = Field(default=None)   # no index, no FK
```

Neither is documented as authoritative, and they drift — `samples.py:181` lazily backfills `owner_user_id` when it finds it null, which means the indexed column is *sometimes* unpopulated while the unindexed one is set.

The practical consequence: **the indexed column is unreliable and the reliable column is unindexed.** A "resources I own" query (`GET /datasets?owner_orcid=<me>`) filters the string column and therefore sequentially scans. This matters because that query is the mechanism replacing personal projects (see Future Work).

The fix is *not* to index `owner_orcid`. It is to make `owner_user_id` trustworthy — backfill it, populate it on every write path — and then resolve ORCID → `user.id` in the route and filter on the existing integer index. No schema addition required.

### 2. `Sample.project_id` has neither index nor FK

```python
# Dataset — indexed and constrained
project_id: Optional[str] = Field(default=None, index=True, foreign_key="project.project_id")
# Sample — neither
project_id: Optional[str] = None
```

`GET /samples?project_id=X` is a sequential scan today. There is no rationale for the asymmetry; Sample should match Dataset.

### 3. No composite index for the dominant listing query

`WHERE project_id = X ORDER BY unique_id DESC LIMIT n` forces the planner to choose between scanning the `project_id` index and sorting everything, or walking `unique_id` and filtering. A composite `(project_id, unique_id)` on `Dataset` serves both — filtered, already ordered, early-terminating under `LIMIT`.

### 4. FK conventions — three variants in use

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
