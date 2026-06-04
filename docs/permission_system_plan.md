# Permission System Redesign

## Goals

- Unified role-based access control (RBAC) at the group level
- Project leads and resource owners can manage their own resources without global admin
- One generic permission check replaces scattered `require_read/write/admin` calls
- Auditable, extensible, consistent across all resource types

---

## Architecture

### Roles

Four roles, ordered by privilege:

| Role | Read | Upload own | Edit others | Delete | Manage group |
|---|---|---|---|---|---|
| `viewer` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `contributor` | ✅ | ✅ | ❌ | ❌ | ❌ |
| `editor` | ✅ | ✅ | ✅ | ❌ | ❌ |
| `admin` | ✅ | ✅ | ✅ | ✅ | ✅ |

- **viewer** — read-only access to group resources
- **contributor** — can upload and fully manage their own resources (via their ORCID group), but cannot edit resources owned by others within the group
- **editor** — can read and write all resources in the group regardless of ownership
- **admin** — full control: read, write, delete, manage group membership

**Platform super-admins are simply `admin` members of the `all` group** (see below). No separate role needed — scope is determined by group membership, not a special role value.

### Table Changes

**`useraccessgrouplink`** — add `role` column:

```sql
ALTER TABLE useraccessgrouplink ADD COLUMN role VARCHAR DEFAULT 'editor';
```

| user_id | accessgroup_id | role |
|---|---|---|
| 42 | 7 (project group) | contributor |
| 42 | 65 (own ORCID group) | admin |
| 1 | 1 (`all` group) | admin |

**`resourceacl`** — `read`/`write` booleans remain for now as a group-level gate. The user's role within the group determines the effective permission level. These can be deprecated later once all access is role-driven.

**New `all` group** — a global group with an ACL entry on every resource. Platform admins are `admin` members of this group, giving them universal access without any special-casing in code.

### Access Check Logic

Replace `require_read_access`, `require_write_access`, `require_admin` with:

```python
require_permission(session, resource_id, user_groups, permission='read')
# user_groups: list of (accessgroup_id, role) tuples for the requesting user
# permission: 'read' | 'write' | 'delete' | 'admin'
```

Logic:
1. Find all `ResourceACL` entries for the resource where `accessgroup_id` is in the user's groups
2. For each matching group, check the user's role in that group
3. Grant access if any group satisfies the required permission level

Role hierarchy: `admin > editor > contributor > viewer`

---

## Resource Creation

At creation, three ACL entries are added automatically:

1. **`all` group** — platform admins get universal access
2. **Owner ORCID group** — owner gets `admin` role on their own resource
3. **Project group** — members' access level determined by their role in the project group

---

## Project Membership

`POST /projects/{proj_id}/users/{orcid}?role=contributor` — role defaults to `contributor`.

Project lead is automatically assigned `admin` role in the project group at project creation.

| Role in project | Can do |
|---|---|
| `viewer` | Read project resources |
| `contributor` | Upload + manage own resources within the project |
| `editor` | Read + write all project resources |
| `admin` | Read + write + delete + manage project membership |

New routes needed:
- `PATCH /projects/{proj_id}/users/{orcid}` — change a member's role
- `GET /projects/{proj_id}/users` — return role alongside user info

---

## What Needs to Change

### Schema
- [ ] Add `role VARCHAR DEFAULT 'editor'` to `useraccessgrouplink`
- [ ] Create `all` access group
- [ ] Migration: set existing `useraccessgrouplink` rows to `role='admin'` (preserves current behavior)
- [ ] Migration: set project leads to `role='admin'` in their project group

### Models
- [ ] `UserAccessGroupLink` ORM — add `role: str = 'editor'`
- [ ] New `RoleEnum`: `viewer`, `contributor`, `editor`, `admin`

### Auth Utils (`app/utils/auth_utils.py`)
- [ ] Add `require_permission(session, resource_id, user_groups_with_roles, permission)` — the unified check
- [ ] Keep `require_admin` as an alias for backwards compat during migration
- [ ] Deprecate `require_read_access`, `require_write_access` — replace call by call

### Resource Creation (`create_standard_acls`)
- [ ] Add `all` group ACL entry at creation
- [ ] Owner ORCID entry with `admin` role
- [ ] Project entry with group-level role (inherited from user's role in project)

### Project Routes (`app/api/routes/projects.py`)
- [ ] `POST /projects/{proj_id}/users/{orcid}` — add `role` param
- [ ] `PATCH /projects/{proj_id}/users/{orcid}` — new route to update role
- [ ] `GET /projects/{proj_id}/users` — include role in response
- [ ] Project creation — assign lead `admin` role in project group

### Deletion
- [ ] Replace global-admin-only deletion with `require_permission(..., 'delete')`
- [ ] Resource owners (`admin` role) and project admins can delete their own resources

### Access Group Routes (`app/api/routes/access_groups.py`)
- [ ] Expose role when listing group members
- [ ] Allow role updates by group admins

---

## Migration Path

1. Add `role` column with default `'editor'`
2. Backfill: set ORCID-named group memberships to `'admin'` (users are always admin of their own group)
3. Backfill: set project lead memberships to `'admin'`
4. Create `all` group, add platform admins as `admin` members
5. Implement `require_permission` alongside existing checks (no routes changed yet)
6. Migrate routes one by one, replacing old checks with `require_permission`
7. Deprecate `read`/`write` booleans on `ResourceACL` once all checks are role-driven
