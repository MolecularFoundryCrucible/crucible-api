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

Platform admins (developers, system operators) are members of a designated **`admin` group**. They bypass resource-level permission checks entirely — no ACL rows needed per resource:

```python
def require_permission(...):
    if any(g.group_name == 'admin' for g in user_groups):
        return True   # platform admin bypass
    # normal ACL + role check...
```

This avoids the operational complexity of maintaining an ACL entry on every resource. Platform admin access is universal by design and does not go through the ACL system.

### Groups

Three types of groups participate in the ACL system:

| Group | Named after | Who is in it | Role in it |
|---|---|---|---|
| Project group | `project_id` | Project members | their project role |
| ORCID group | user's ORCID | The user themselves | `owner` |
| `admin` group | `admin` | Platform admins | `admin` (+ bypass) |

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

## Instrument Service Accounts

> **Under discussion** — see open questions below.

Each instrument may have an associated service account (`User` with `is_service_account=True`) representing the software running on the instrument. These accounts need rights to upload data and manage instrument metadata without being platform admins.

Proposed design mirrors the ORCID group pattern:

- Each instrument gets its own **instrument group** (named after instrument MFID)
- The instrument's service account is a member with `owner` role in that group
- The instrument resource itself has that group in its ACL
- When an instrument is associated with a project, it gets added to the project group with `editor` role

**Open questions:**
- How is a service account linked to an instrument? New route: `POST /instruments/{id}/service_account`?
- Can an instrument be associated with multiple projects simultaneously?
- What happens to the instrument group's project access when the instrument is decommissioned?
- Should instrument service accounts be able to create datasets autonomously, or only ingest into pre-created ones?

---

## What Needs to Change

### Schema
- [ ] Add `role VARCHAR DEFAULT 'editor'` to `useraccessgrouplink`
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

### Access Group Routes
- [ ] Expose role when listing group members (`GET /access_groups/{name}/users`)
- [ ] Allow role updates by group admins

---

## Migration Path

1. Add `role` column with default `'editor'`
2. Backfill: set ORCID-named group memberships to `owner`
3. Backfill: set project lead memberships to `admin`
4. Implement `require_permission` alongside existing checks (no routes changed yet)
5. Migrate routes one by one, replacing old checks with `require_permission`
6. Drop `read`/`write` booleans from `resourceacl` once all checks are role-driven
