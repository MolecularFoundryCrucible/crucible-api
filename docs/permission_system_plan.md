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
| `read_only` | read-only | **dropped** — unused, not replaced | — |

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
| `public` group | `public` | **Virtual** — every principal, always at `viewer` |

> **Migration note.** Instrument access groups are currently named after `instrument_name.lower()` (`datasets.py:195`), not the instrument MFID. Renaming them to MFIDs is a data migration touching existing ACL rows, not a greenfield choice. Either keep the name-based convention or schedule the rename explicitly.

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

one probe per row against the covering index, well under a millisecond for a 100-row page. On write, `public: true` is translated by the route into the ACL row. It is deliberately *not* kept as a stored column alongside the ACL row — that would recreate the two-sources-of-truth drift documented in `docs/schema_hygiene.md`.

**It also collapses an existing inconsistency.** There are currently *two* public flags, and different code paths consult different ones:

| Column | Indexed | Access-filter sites |
|---|---|---|
| `Dataset.public` | yes | 14 — `files.py`, `ingestion_requests.py`, and others |
| `Resource.public` (`idtype`) | no | 3, including `read_access_filter_list` |
| `Sample` — no column | — | samples rely solely on `Resource.public` |

They are kept in step by hand (`ri.public = ds.public` on create and update). Nothing enforces it, so a drift makes a dataset visible through `GET /files` but not `GET /datasets`, or the reverse — an authorization inconsistency, not merely duplicated state. One ACL row replaces both columns and leaves a single code path.

Two edges to handle: `GET /access_groups/public/users` returns empty, since membership is virtual; and anything iterating `principal.groups` — "which projects am I in", for instance — must exclude the public group.

---

## Platform Access

### Platform admins

Platform admins bypass all resource-level checks. This is a flag on `User`, not group membership — groups control resource access, user flags control system-level bypass.

```sql
ALTER TABLE "user" ADD COLUMN is_platform_admin BOOLEAN DEFAULT FALSE;
```

The flag travels on the `Principal` (see Implementation → Auth), so the check needs no extra query:

```python
def require_permission(principal, resource_id, permission='read'):
    if principal.is_platform_admin:
        return True
    # effective-role check against resourceacl
```

**Stop materializing bypass access.** `create_standard_acls` currently writes an ACL row for the `admin` group and the `read_only` group on every resource created. The `admin` row is dead weight under this model — platform admins never consult the ACL.

`read_only` is dropped outright rather than converted. It is not in use, so there is nothing to preserve, and its ACL rows are the only place in the schema where `write=False` appears systematically — removing them is what makes the backfill a single uniform assignment rather than a mapping (see Migration path). If a platform-wide read-only auditor is wanted later, it should be designed as a flag and a bypass, not as a group with a row on every resource, per the invariant above.

Removing both drops per-resource ACL rows from roughly six to three and eliminates two inserts per resource creation.

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

**The one real gap is presentational.** If the UI organises everything by project, unattached data has nowhere to appear. That is a "My data" view, not a table: query resources whose owner is the caller. Note this must filter on the indexed `owner_user_id`, not `owner_orcid` — see `docs/schema_hygiene.md`, item 1, since `owner_user_id` needs to be made reliable first.

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
| setting `public = true` | none | resolved by construction — see below |
| `instrument_name` on `POST /datasets` | none | validate against the `Instrument` table |

The instrument ones are the most exposed: nothing currently prevents any authenticated user from renaming or re-pointing someone else's instrument.

**Publishing is the subtlest, and the redesign closes it without adding a check.** Today `public` is a plain pass-through field (`ri.public = ds.public`, `datasets.py:185` and `samples.py:198`) with no gate at all, so any user who can create or edit a resource can make it world-readable — a contributor on confidential or industry-partnered work can publish unilaterally. Once publishing becomes an ACL entry against the `public` group (see Core Concepts → Public resources), it is an ACL-management operation and therefore already requires `admin` on the resource. The gap closes as a consequence of the model rather than as a special case.

**Unvalidated `instrument_name` creates access groups.** `create_dataset` looks the name up in the `Instrument` table (`datasets.py:164`) and sets `instrument_id = None` when there is no match — but then creates an access group from the raw string anyway (`datasets.py:197-201`) and writes an ACL entry for it. So `POST /datasets {"instrument_name": "xrd-typo"}` silently produces a permanent access group named `xrd-typo`. A typo pollutes the group table and attaches a meaningless ACL entry. The name should be validated against registered instruments.

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

This is a correctness bug independent of the redesign, but it lands squarely in scope, because these are exactly the routes being migrated to `require_permission`. Each reassignment path needs a matching revoke, and the revoke must be careful: removing the old project's ACL entry is correct, but removing the old *owner's* entry is an ownership transfer and should follow whatever rule Future Work settles for that — not silently drop it as a side effect of a `PATCH`.

Until then, the safest interim behaviour is to reject an `owner_orcid` change through `PATCH` altogether and require the eventual explicit transfer route, rather than continue silently accumulating owners.

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
- [ ] `accessgroup` — insert the virtual `public` group row
- [ ] `idtype` — drop `public` after the ACL rows are backfilled
- [ ] `dataset` — drop `public` **and its index** (bare boolean index, no selectivity)

### Indexing

> Table-level schema issues found during this audit — the `owner_orcid`/`owner_user_id` duplication, `Sample.project_id` lacking an index and FK, and the FK-convention inconsistencies — are recorded separately in `docs/schema_hygiene.md`. They are cleanup with their own migration risk and are not part of this redesign.

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
    is_platform_admin: bool
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

Every downstream use of both variables continues to work untouched, so this lands as a single reviewable commit where every hunk has the same shape. Routes then adopt `principal.is_platform_admin` and `principal.role_in(...)` gradually as they migrate to `require_permission`, deleting the destructuring line as they go.

The distinction that matters: a mechanical 106-site change is safe and reviewable; a semantic one is not. The incremental promise of this migration applies to permission *logic*, not to the type of the auth object — trying to keep the tuple shape forever costs more than changing it once.

#### The `MIN` costs nothing on the read path

Because `min(a,b) >= r ⟺ a >= r AND b >= r`, and per-group roles are already in the principal, "which of my groups clear the bar" is computed in Python and SQL sees only a single column comparison:

```python
qualifying = principal.groups_at_or_above(required)
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

### Testing

A change touching 70+ authorization call sites currently has **no regression coverage on authorization at all**, and the suite it would land in is already red.

#### Current state

| File | Reality |
|---|---|
| `test_datasets.py`, `test_users.py` | empty — 0 lines |
| `test_graph_diff.py`, `test_graph_speed.py` | scripts and benchmarks; 0 test functions, collected anyway |
| `test_datasets_endpoints.py` | 21 failed / 1 passed — mocks `app.api.main.Session`, which no longer exists |
| `test_gcs_multipart_endpoint.py` | 6 errors on collection |
| `test_username_utils.py` | 19 passed / 1 failed |
| `test_associated_file_remote_backend.py` | 8 passed |

Totals: **28 passed, 22 failed, 6 errors.** Four real test files, two of them broken.

The single username failure is a stale test, not a bug — `validate_username_format` lowercases before validating, so uppercase input normalizes rather than raising, which is the intended case-normalization behaviour. The assertion needs updating, one line.

#### Red as the normal state is the actual problem

With 22 failures and 6 errors as the baseline, a new break is indistinguishable from background noise. Adding good permission tests to a suite that is already red buys very little, because the signal is gone. **Reaching green is a precondition**, and most of the route there is deletion rather than repair:

- [ ] delete the two empty files
- [ ] move the two script/benchmark files out of `tests/` so pytest stops collecting them
- [ ] fix the stale username assertion
- [ ] quarantine `test_datasets_endpoints.py` and `test_gcs_multipart_endpoint.py` with a stated reason — they mock a module structure that no longer exists, so they are artifacts rather than tests pending an update. Rewriting them against the current fixtures is genuine work and should not block this.

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

**Integration, thin — one per threshold class, not one per route.** Viewer reads but cannot edit; contributor creates, edits own, cannot edit others'; editor edits others' but cannot delete; admin deletes; service account creates on behalf but cannot read outside its projects. The `auth_as` fixture in `tests/conftest.py` already fakes group membership and is the right foundation.

**Agreement tests, specific to the migration.** For a matrix of `(principal, resource, operation)`, assert the old check and the new check return the same answer, with a deliberate enumerated list of exceptions where behaviour is *supposed* to change — for example project members losing the ability to add other members. This is the test-time counterpart to shadow-mode validation, and it turns "did this break something across 70 routes" into a question with an answer.

### Migration path

~70 call sites use the four existing check helpers (`require_groups` 21, `require_write_access` 16, `require_admin` 12, `read_access_filter_list` 12, `require_read_access` 9). The risk in this project is the migration, not the model.

**Routes are cut over all at once, not one at a time.** Two permission systems coexisting is its own hazard — anyone reading the code has to know which routes are on which check, and that ambiguity lasts as long as the migration does. A single maintainer on a rollback-capable deployment does not need per-route staging; what is needed instead is confidence *before* the cutover, which steps 4–6 provide.

0. **Get the test suite green** (see Testing) — delete the empty files, move the benchmark scripts out, fix the stale username assertion, quarantine the two broken files. Nothing after this is verifiable until the baseline is clean.
1. Add columns and the enum; backfill:
   - every existing project-group membership → `editor`
   - ORCID-group memberships → `owner`
   - project leads → `admin`
   - drop `read_only` ACL rows; drop `admin`-group ACL rows
   - remaining ACL rows → `admin` for project groups, `contributor` for instrument groups, `owner` for ORCID groups
   - create the virtual `public` group; for every resource with `public=true`, insert `(mfid, public_group, viewer)`; then drop the `Resource.public` column
2. Set `is_platform_admin=true` for existing platform admins
3. **Introduce `Principal`** — one mechanical commit rewriting all 106 destructuring lines to `auth_res.user_id, auth_res.groups`. No logic changes, every hunk identical. Land and verify on its own.
4. Add the pure `effective_role` and its exhaustive unit matrix, plus `require_permission` — not yet wired to any route
5. Write the agreement tests: old check vs new check across the matrix, with deliberate exceptions enumerated
6. **Run shadow mode** until mismatches settle to only the enumerated exceptions (see below)
7. **Cut over.** Replace all check call sites in one deploy, converging inline ACL filters onto the helper and dropping the destructuring lines
8. Add create-on-behalf verification; relax the directory route; close unguarded operations
9. Fix reassignment to revoke stale ACL entries (see Reassignment Never Revokes)
10. Add the covering index; drop `read`/`write` and their indexes

#### Why the backfill is a uniform assignment, not a mapping

Every ACL row `create_standard_acls` writes is `read=True, write=True`. The sole systematic exception is `read_only`, and those rows are being deleted. The only remaining unknown is manually created entries via `POST /resources/{mfid}/access_groups`, which takes `read` and `write` as caller-supplied parameters — worth one query to confirm the set is empty before relying on this:

```sql
SELECT read, write, count(*) FROM resourceacl GROUP BY read, write;
```

If it returns only `(t,t)` and `(t,f)` — the latter being `read_only` — the backfill needs no conditional logic at all.

#### Existing members become `editor`, not `contributor`

This preserves current behaviour exactly. Today the project group holds `write=True` on every project resource and `require_write_access` passes if *any* of your groups has write — so **any project member can currently edit any project resource**, including colleagues' work. Backfilling to `contributor` would silently remove that from every user on day one.

`editor` is the neutral choice: nobody's effective access changes at cutover. New memberships still default to `contributor` per `POST /projects/{proj_id}/users/{orcid}?role=`, so the stricter model applies going forward, and project admins demote existing members deliberately rather than by migration side effect.

#### Shadow mode

Cutting over in one deploy makes pre-cutover evidence essential. Before step 7, compute both answers on every request, act on the old one, and log where they differ:

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

On performance, the join is roughly a wash rather than a regression, provided the index is right. Because `resource_project_link.resource_mfid` *is* the value listings order by, an index on `(project_id, resource_mfid)` yields project-filtered rows already in output order from a two-column table; take the page, then join to `dataset` by primary key. Which side wins depends on selectivity — today's column filter is fine when your project dominates recent data, the link index is better when it is a small slice. Note that the composite index today's query actually wants, `(project_id, unique_id)` on `Dataset`, does not exist either (see `docs/schema_hygiene.md`, item 3).

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

### Ownership transfer

When an owner leaves, their resources remain under their ORCID group, and only they can modify that ACL entry. Open questions: who may initiate a transfer (platform admins only, or project admins too); whether the previous owner retains a lower role; whether ownership can transfer to a group rather than an individual.

### Projects as first-class resources

`create_project()` does not create a `Resource` row, so projects have no MFID entry, no `creation_time`/`modification_time`, and cannot carry ACL entries or scientific metadata the way datasets and samples do. Fixing this touches ACL and deletion-filter assumptions that key off `Resource` rows existing.

### Authorizing on-behalf-of at shared machines

Service accounts already avoid researchers typing API keys into lab computers, but the SA's assertion that it is acting for a given researcher is currently just trusted. The standard solution is the OAuth 2.0 Device Authorization Grant (RFC 8628), designed for exactly this input-constrained shared-device case: the researcher approves the session from their own phone, and the instrument receives a short-lived token scoped to that user. Worth considering if the trust placed in instrument software needs hardening.

### Permission-change auditing

Platform-admin bypass and ACL modifications are currently indistinguishable from ordinary authorized actions in any audit trail. A `ResourceAuditLog` table — one row per mutation with actor, action, and target — would cover this and the broader "who changed what" gap that `mf-record-prod` snapshots only partially address.

### Project identity: mfid vs `project_id`

`project_id` currently does two jobs at once — it is the stable identity that every foreign key points at, *and* the human-readable label that appears in URLs and UI. Those pull in opposite directions the moment an admin wants to rename a project, and the model already solves this correctly one table over: `User` separates `unique_id` (ORCID, stable, what FKs reference) from `username` (mutable, unique, what people type). Projects should follow the same split.

This converges with making projects first-class resources. A `Resource` row is keyed by `mfid`, and an mfid is immutable by definition — so a renameable `project_id` *cannot* be the mfid. Generating an mfid for projects and adding them to `idtype` is therefore one change, not two, and it is a prerequisite for renaming rather than a separate nicety.

What a rename would touch, once identity moves to the mfid:

| Reference | Today | After |
|---|---|---|
| `Dataset.project_id`, `Sample.project_id` | FK → `project.project_id` | FK → `project.unique_id` |
| `UserProjectLink.project_id` | FK → `project.project_id` | FK → `project.unique_id` |
| `AccessGroup.group_name` for project groups | equals `project_id` | equals the project mfid |
| `AccessGroupJoinRequest.group_name` | FK → `accessgroup.group_name` | unchanged, follows the group |
| `ResourceACL` | references `accessgroup.id` (int) | **unaffected** — already stable |

`ResourceACL` needing no change is the useful part: ACL rows point at `accessgroup.id`, so renames never touch the permission data itself.

Naming project access groups after the mfid rather than the label is what makes renaming free, but it also means every `require_groups(ag_list, {'admin', proj_id})` comparison changes shape. Those call sites are being rewritten for `require_permission` anyway, so the two migrations should be sequenced together rather than done twice.

One open question worth settling before implementation: **what do routes take?** `/projects/{mfid}` is stable but unreadable; `/projects/{project_id}` stays readable but breaks bookmarks on rename. The `User` precedent suggests offering both — a canonical path on the stable id plus a lookup route on the label, mirroring `GET /users/by-username/{username}`. Renames would also want the old label to 404 rather than silently resolve to a different project, which argues for never reusing a retired `project_id`.

Until this lands, `project_id` must be treated as immutable in practice — there is no rename path today, and adding one before identity is separated would break every foreign key pointing at it.
