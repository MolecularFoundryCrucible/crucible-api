# Permission System Redesign

## Goals

- Unified role-based access control (RBAC) at the group level
- Project leads and resource owners can manage their own resources without global admin
- One generic permission check replaces scattered `require_read/write/admin` calls
- Auditable, extensible, consistent across all resource types

---

## Architecture

### Roles

Five roles, ordered by privilege:

| Role | Read | Upload own | Edit others | Delete | Manage ACL |
|---|---|---|---|---|---|
| `viewer` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `contributor` | ✅ | ✅ | ❌ | ❌ | ❌ |
| `editor` | ✅ | ✅ | ✅ | ❌ | ❌ |
| `admin` | ✅ | ✅ | ✅ | ✅ | ✅ (below owner) |
| `owner` | ✅ | ✅ | ✅ | ✅ | ✅ (below platform admin) |

Role hierarchy: `platform_admin > owner > admin > editor > contributor > viewer`

- **viewer** — read-only access to group resources
- **contributor** — can create and fully manage their own resources, cannot edit resources created by others
- **editor** — can read and write all resources in the group regardless of ownership
- **admin** — full control within the group: read, write, delete, manage ACL entries at or below admin level
- **owner** — structural role, tied to the resource creator. Can manage all ACL entries on their resource except the platform admin bypass. Cannot be demoted by project admins.

### Platform Admins

Platform admins (developers, system operators) bypass resource-level permission checks entirely — no ACL rows needed per resource. Rather than checking by group name (fragile, breaks on rename), two boolean flags on `AccessGroup` drive the bypass:

| Flag | Meaning |
|---|---|
| `is_platform_admin` | Members bypass all permission checks |
| `is_service_account_group` | Members get read-only bypass + elevated route allowlist |

```python
def require_permission(...):
    if any(g.is_platform_admin for g in user_groups):
        return True   # platform admin — full bypass
    if permission == 'read' and any(g.is_service_account_group for g in user_groups):
        return True   # service account — read-only bypass
    # normal ACL + role check...
```

The `admin` and `service_accounts` groups have these flags set at creation. Additional groups can be granted the same bypass in future without code changes.

### Groups

Four types of groups participate in the ACL system:

| Group | Named after | Who is in it | Role in it | Flag |
|---|---|---|---|---|
| Project group | `project_id` | Project members | their project role | — |
| ORCID group | user's ORCID | The user themselves | `owner` | — |
| `admin` group | `admin` | Platform admins | `admin` | `is_platform_admin=True` |
| `service_accounts` group | `service_accounts` | Instrument service accounts | `admin` | `is_service_account_group=True` |
| Instrument group | instrument MFID | Service account (`admin`), human admins (`admin`/`owner`) | see above | — |

#### ORCID groups as ownership
Every user has a personal group named after their ORCID. At resource creation, this group is added to the resource's ACL with the `owner` role. This is what makes you the owner of your resource — no separate ownership field needed in the permission check.

### Table Changes

**`useraccessgrouplink`** — add `role` column:

```sql
ALTER TABLE useraccessgrouplink ADD COLUMN role VARCHAR DEFAULT 'editor';
```

| user_id | accessgroup_id | role |
|---|---|---|
| 42 | 7 (project group) | contributor |
| 42 | 65 (own ORCID group) | owner |
| 1 | 1 (`admin` group) | admin |

**`resourceacl`** — `read`/`write` booleans are redundant under this model and will be dropped. Presence of an ACL entry plus the user's role in the matching group determines effective access.

### Access Check Logic

Replace `require_read_access`, `require_write_access`, `require_admin` with:

```python
require_permission(session, resource_id, user_groups, permission='read')
# user_groups: list of (accessgroup, role) tuples for the requesting user
# permission: 'read' | 'write' | 'delete' | 'admin' | 'owner'
```

Logic:
1. Platform admin bypass — if caller is in the `admin` group, grant immediately
2. Find all `ResourceACL` entries for the resource where `accessgroup_id` is in the user's groups
3. For each matching group, get the user's role in that group from `useraccessgrouplink`
4. Grant access if any group's role satisfies the required permission level

### ACL Management Hierarchy

A user can only manage ACL entries at or below their own effective role on the resource:

- `owner` can add/remove/change any ACL entry except the `admin` group bypass (immutable)
- `admin` (project level) can manage ACL entries for `editor`, `contributor`, `viewer` — cannot touch the owner's ORCID group entry
- `editor` and below cannot manage ACL entries

The `admin` group entry is always immutable — no user can remove platform admin access regardless of their role.

---

## Resource Creation

At creation, two ACL entries are added automatically:

1. **Owner's ORCID group** — creator gets `owner` role on their resource
2. **Project group** — members' effective access determined by their role in the project group

Platform admins access all resources via the bypass — no ACL entry needed.

---

## Sharing

Primary sharing mechanism: **project groups**. Adding a user to a project grants them access to all project resources at their assigned role level.

Optional per-resource sharing: an owner or project admin can share a single resource with an individual outside the project. This adds one ACL entry pointing to the recipient's ORCID group with the specified role. The route resolves ORCID → ORCID group internally — the caller thinks in terms of people, not groups.

```
POST /resources/{id}/share
Body: { "orcid": "0000-...", "role": "viewer" }

DELETE /resources/{id}/share/{orcid}
```

Per-resource shares are intentionally an escape hatch — the primary model is project-based.

---

## Project Membership

`POST /projects/{proj_id}/users/{orcid}?role=contributor` — role defaults to `contributor`.

Project lead is automatically assigned `admin` role in the project group at project creation.

| Role in project | Can do |
|---|---|
| `viewer` | Read project resources |
| `contributor` | Create + manage their own resources within the project |
| `editor` | Read + write all project resources |
| `admin` | Read + write + delete + manage project membership and ACLs |

Routes:
- `POST /projects/{proj_id}/users/{orcid}?role=` — add member with role
- `PATCH /projects/{proj_id}/users/{orcid}?role=` — change a member's role
- `GET /projects/{proj_id}/users` — return members with their roles
- `DELETE /projects/{proj_id}/users/{orcid}` — remove member

---

## Ownership Transfer

> **Deferred — needs design decision.**

When a resource owner leaves (e.g. a researcher moves institutions), their datasets remain owned by their ORCID group. Under the current model, only the owner can modify their ORCID group ACL entry, and platform admins bypass ACL checks but have no route to reassign ownership.

A future ownership transfer mechanism is needed. Open questions to resolve:
- Who can initiate a transfer — platform admins only, or also project admins?
- Does the previous owner lose access after transfer, or retain a lower role (e.g. `editor`)?
- Can ownership be transferred to a project group rather than an individual?

---

## Instrument Service Accounts

> **Draft design — to be revisited when implementation begins.**

Instrument service accounts are `User` records with `is_service_account=True`, representing software running on a physical instrument. They need to read broadly, write to associated projects, and act as agents on behalf of researchers — without having full platform admin rights.

### `service_accounts` group

A dedicated `service_accounts` group (analogous to the `admin` group) grants two things:

1. **Read-only bypass** — members can read all resources without an ACL entry. No need to add the instrument to every new project just to see its data.
2. **Allowlisted elevated routes** — routes normally restricted to admins but safe for instrument automation (e.g. `POST /users/resolve`, project/sample reads for orchestration).

```python
def require_permission(..., permission='read'):
    if any(g.group_name == 'admin' for g in user_groups):
        return True  # full bypass
    if permission == 'read' and any(g.group_name == 'service_accounts' for g in user_groups):
        return True  # read-only bypass for service accounts
    # normal ACL + role check...
```

Write operations (dataset creation, ingestion requests) are NOT bypassed — they require explicit project association.

### Instrument group

Each instrument gets its own group (named after instrument MFID). The instrument resource has that group in its ACL. Group membership tiers:

| Role in instrument group | Who | Can do |
|---|---|---|
| `owner` | Instrument creator / registrar | Full management, decommission, transfer ownership |
| `admin` | Beamline scientists, facility staff | Edit metadata, change status, manage service accounts, manage project associations |
| `admin` | Instrument service account | Update instrument metadata programmatically (not status, not group membership) |

The service account holds `admin` (not `owner`) in the instrument group. This is intentional — service accounts should not be able to decommission themselves or manage human membership in the group.

### Instrument-project association

Instruments are explicitly associated with the projects they actively serve. When the association is created (by a project admin or scheduling system), the service account automatically receives `contributor` role in the project group. When removed, access is revoked.

```
POST   /instruments/{id}/projects/{proj_id}   # associate + auto-grant contributor access
DELETE /instruments/{id}/projects/{proj_id}   # disassociate + revoke access
```

An instrument can be associated with multiple projects simultaneously.

### Dataset ownership

Datasets created by an instrument service account are owned by the **researcher**, not the instrument. The `owner_orcid` is passed in the creation payload; the ownership ACL entry uses the researcher's ORCID group. The instrument is the technical creator but the researcher retains legal ownership.

### Capability summary

| Capability | Mechanism |
|---|---|
| Read all data | `service_accounts` group → read bypass |
| Create datasets in project | `contributor` role via instrument-project association |
| Assign researcher as owner | `owner_orcid` in creation payload |
| Request ingestion | `contributor` role via instrument-project association |
| Manage instrument metadata | `owner` role in instrument group |
| Resolve users / elevated lookups | `service_accounts` group allowlist |

### Instrument status

Instruments have a `status` column rather than being deleted. Statuses:

| Status | Meaning | Service account write access |
|---|---|---|
| `active` | Operational | Yes (via project associations) |
| `maintenance` | Temporarily offline | Yes (kept active, maintenance is transient) |
| `decommissioned` | Permanently retired | No — all project associations auto-revoked |

`GET /instruments` defaults to `status=active`. Pass `?status=decommissioned` to include retired instruments.

Only instrument `owner`, instrument `admin`, or platform admins can change status. Decommissioning automatically removes all instrument-project associations (revoking the service account's `contributor` role in project groups). The instrument resource, its metadata, and all datasets it produced are fully preserved.

The instrument group membership is retained after decommissioning for audit purposes — the service account remains in the group but loses project write access.

---

## What Needs to Change

### Schema
- [ ] Add `role VARCHAR DEFAULT 'editor'` to `useraccessgrouplink`
- [ ] Add `is_platform_admin BOOLEAN DEFAULT FALSE` to `accessgroup`
- [ ] Add `is_service_account_group BOOLEAN DEFAULT FALSE` to `accessgroup`
- [ ] Set `is_platform_admin=True` on the `admin` group
- [ ] Set `is_service_account_group=True` on the `service_accounts` group (create if missing)
- [ ] Migration: set ORCID-named group memberships to `owner`
- [ ] Migration: set project lead memberships to `admin`
- [ ] Migration: set `admin` group memberships to `admin`
- [ ] Drop `read`/`write` booleans from `resourceacl` (after all checks are role-driven)

### Models
- [ ] `UserAccessGroupLink` ORM — add `role: str = 'editor'`
- [ ] `RoleEnum`: `viewer`, `contributor`, `editor`, `admin`, `owner`

### Auth Utils (`app/utils/auth_utils.py`)
- [ ] Add `require_permission(session, resource_id, user_groups_with_roles, permission)` — the unified check with platform admin bypass
- [ ] Keep `require_admin` as an alias during migration
- [ ] Deprecate `require_read_access`, `require_write_access` — replace call by call

### Resource Creation (`create_standard_acls`)
- [ ] Owner ORCID entry with `owner` role
- [ ] Project entry (no platform admin ACL entry needed)

### Resource Sharing
- [ ] `POST /resources/{id}/share` — owner or project admin only
- [ ] `DELETE /resources/{id}/share/{orcid}` — owner or project admin only

### Project Routes (`app/api/routes/projects.py`)
- [ ] `POST /projects/{proj_id}/users/{orcid}` — add `role` param
- [ ] `PATCH /projects/{proj_id}/users/{orcid}` — new route to update role
- [ ] `GET /projects/{proj_id}/users` — include role in response
- [ ] Project creation — assign lead `admin` role in project group

### Deletion
- [ ] Replace global-admin-only deletion with `require_permission(..., 'delete')`
- [ ] Resource owners and project admins can delete resources within their scope

### Instruments
- [ ] Add `status VARCHAR DEFAULT 'active'` to `instrument` table (`active`, `maintenance`, `decommissioned`)
- [ ] `GET /instruments` — filter by `status=active` by default, expose `?status=` param
- [ ] `PATCH /instruments/{id}` — status change requires instrument `admin`/`owner` or platform admin
- [ ] `POST /instruments/{id}/projects/{proj_id}` — associate instrument with project, auto-grant `contributor` to service account
- [ ] `DELETE /instruments/{id}/projects/{proj_id}` — disassociate, auto-revoke `contributor`
- [ ] Decommissioning auto-revokes all instrument-project associations
- [ ] At instrument creation, create instrument group and add creator as `owner`, service account as `admin`

### Access Group Routes
- [ ] Expose role when listing group members (`GET /access_groups/{name}/users`)
- [ ] Allow role updates by group admins

---

## Migration Path

1. Add `role VARCHAR DEFAULT 'contributor'` to `useraccessgrouplink`
2. Backfill `useraccessgrouplink` roles using the current `resourceacl` booleans:
   - `read=true, write=false` → `viewer`
   - `read=true, write=true` → `contributor`
   - ORCID-named group memberships → `owner` (override regardless of booleans)
   - Project lead memberships → `admin` (override regardless of booleans)
3. Implement `require_permission` alongside existing checks (no routes changed yet)
4. Migrate routes one by one, replacing old checks with `require_permission`
5. Drop `read`/`write` booleans from `resourceacl` once all checks are role-driven

> **Note:** Existing users with `write=true` are conservatively mapped to `contributor` rather than `editor`. Project leads can promote individual users via `PATCH /projects/{proj_id}/users/{orcid}?role=editor` on a case-by-case basis.
