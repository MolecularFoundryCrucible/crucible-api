# Permission System Redesign

## Goals

The current permission model is binary: read or write access to a group's resources. This is too coarse for a platform where researchers, collaborators, facility staff, instrument software, and operators all have different levels of responsibility.

The redesign adds five roles (`viewer`, `contributor`, `editor`, `admin`, `owner`) applied at the group level. Permissions are scoped by context: your role in a project group governs what you can do to project resources; your role in your ORCID group covers your own resources; platform admin access is a separate bypass outside the ACL system.

---

## Architecture

### Roles

To determine a user's access on a resource:

1. Find all access groups the user belongs to that also have an ACL entry on that resource
2. Check the user's role in each of those groups
3. The highest role wins

Five roles, in order:

| Role | Read | Write | Edit others | Delete | Manage ACL |
|---|---|---|---|---|---|
| `viewer` | ✓ | ✗ | ✗ | ✗ | ✗ |
| `contributor` | ✓ | ✓ | ✗ | ✗ | ✗ |
| `editor` | ✓ | ✓ | ✓ | ✗ | ✗ |
| `admin` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `owner` | ✓ | ✓ | ✓ | ✓ | ✓ |

Hierarchy: `owner > admin > editor > contributor > viewer`

- **viewer**: read-only
- **contributor**: can create and manage their own resources; cannot edit resources created by others
- **editor**: can read and write/edit all resources in the group regardless of who created them
- **admin**: full control within the group; can manage ACL entries at or below admin level
- **owner**: the highest non-platform role. Automatically assigned in a user's ORCID group at resource creation. The permission check treats it the same as any other role: if your ORCID group has an ACL entry on this resource and your role in that group is `owner`, you have owner-level rights.

### Platform Admins and Service Accounts

System-level roles are properties of the `User` record, not group relationships. This keeps a clear separation: groups control resource-level access, user flags control system-level bypass.

Two flags on `User`:

| Flag | Effect |
|---|---|
| `is_platform_admin` | Bypasses all permission checks |
| `is_service_account` | Read-only bypass + elevated route allowlist (already exists) |

`authorize_user` is updated to return the `User` object alongside the group list:

```python
def authorize_user(credentials) -> tuple[User, list[AccessGroup]]:
    ...
    return user, ag_list
```

The permission check then reads directly from the user object (no extra query, since `authorize_user` already fetches the `User` row to load groups):

```python
def require_permission(user, resource_id, user_groups, permission='read'):
    if user.is_platform_admin:
        return True
    if permission == 'read' and user.is_service_account:
        return True
    # normal ACL + role check
```

#### Example: platform admin accessing a dataset

**`user` table:**
```
id | orcid                 | is_platform_admin | is_service_account
42 | 0000-0001-2345-6789   | true              | false
```

**`accessgroup` table:**
```
id | group_name
1  | admin
2  | 0000-0001-2345-6789   ← personal ORCID group
3  | 10k_perovskites       ← a project group
```

**`useraccessgrouplink` table:**
```
user_id | accessgroup_id | role
42      | 1              | admin
42      | 2              | owner
42      | 3              | editor
```

When this user makes a request, `authorize_user` fetches the `User` row and their groups. `require_permission` checks `user.is_platform_admin = True` and returns immediately. `resourceacl` is never consulted.

### Groups

| Group | Named after | Members | Role in group |
|---|---|---|---|
| Project group | `project_id` | Project members | their project role |
| ORCID group | user's ORCID | The user | `owner` |
| `admin` group | `admin` | Platform admins | `admin` |
| `service_accounts` group | `service_accounts` | Instrument service accounts | `admin` |
| Instrument group | instrument MFID | Service account (`admin`), human admins (`admin`/`owner`) | see Instruments section |

The `admin` and `service_accounts` groups still exist for organizing membership and for admin-gated route checks (e.g. `require_admin`), but they no longer drive the permission bypass — `is_platform_admin` and `is_service_account` on `User` do.

#### ORCID groups and ownership

Every user has a personal group named after their ORCID. At resource creation, this group is added to the resource's ACL with the `owner` role.

There is no separate ownership concept in the permission check. Ownership is a consequence of having `owner` role in a group that has an ACL entry on the resource. The same check that resolves a project member's `editor` rights resolves a creator's `owner` rights.

### Table Changes

```sql
ALTER TABLE useraccessgrouplink ADD COLUMN role VARCHAR DEFAULT 'contributor';
ALTER TABLE "user" ADD COLUMN is_platform_admin BOOLEAN DEFAULT FALSE;
```

The `read`/`write` booleans on `resourceacl` will be dropped. Presence of an ACL entry plus the user's role in the matching group is sufficient.

### Access Check

```python
require_permission(user, resource_id, user_groups, permission='read')
# user: User ORM object
# user_groups: list of AccessGroup objects the user belongs to
# permission: 'read' | 'write' | 'delete' | 'admin' | 'owner'
```

1. If `user.is_platform_admin`, grant immediately
2. If `permission == 'read'` and `user.is_service_account`, grant immediately
3. Find all `ResourceACL` entries where `accessgroup_id` is in the user's groups
4. Get the user's role in each matching group from `useraccessgrouplink`
5. Grant if any role satisfies the required permission level

### ACL Management

A user can only manage ACL entries at or below their own effective role:

- `owner` can add/remove/change any entry except the platform admin bypass (immutable)
- `admin` can manage entries for `editor`, `contributor`, `viewer` — cannot touch the owner's ORCID group entry
- `editor` and below cannot manage ACL entries

---

## Resource Creation

At creation, two ACL entries are added:

1. Owner's ORCID group with `owner` role
2. Project group(s) — members get access at their project role. If no project is specified, the resource goes to the creator's personal project.

---

## Multi-Project Resources

A resource can belong to more than one project. The single `project_id` field on `Dataset` and `Sample` is replaced by a relationship table:

```sql
CREATE TABLE resource_project_link (
    resource_mfid VARCHAR REFERENCES idtype(mfid),
    project_id    VARCHAR REFERENCES project(project_id),
    PRIMARY KEY (resource_mfid, project_id)
);
```

Filtering by project becomes a join. ACL entries are added for each linked project group at creation.

---

## Personal Projects

Every user gets a personal project created at registration. Resources with no project specified go there.

- `Project.is_personal = True` distinguishes personal from formal projects
- Not shown in `GET /projects` for other users
- Access group is the user's ORCID group — no extra setup needed
- `project_id` is the user's ORCID — stable, never shown to users directly
- `title` (e.g. "Fabrice's workspace") is what the UI displays
- Cannot be deleted

This makes `project_id` always set, enabling a non-null FK constraint.

---

## Data Model Integrity

Currently `owner_orcid` and `project_id` on `Dataset` and `Sample` are plain strings with no FK enforcement. This allows references to users or projects that don't exist.

Changes:
- `Dataset.owner_orcid` → FK to `user.orcid` (non-null)
- `Sample.owner_orcid` → FK to `user.orcid` (non-null)
- `resource_project_link.project_id` → FK to `project.project_id`
- `resource_project_link.resource_mfid` → FK to `idtype.mfid`

A resource cannot be created without a registered owner or assigned to a non-existent project. Deleting a user or project requires handling linked resources first.

---

## User Resolution

The API always operates on ORCIDs internally. Usernames and emails are resolved at the client/UI layer before making API calls.

`POST /users/resolve` and `GET /users/by-username/{username}` are the resolution endpoints. The API never accepts a username as a resource or project identifier.

---

## Sharing

Primary mechanism: project groups. Adding a user to a project gives them access to all project resources at their role level.

Per-resource sharing is available as an escape hatch: an owner or project admin can share a single resource with someone outside the project. This adds one ACL entry pointing to the recipient's ORCID group.

```
POST /resources/{id}/share
Body: { "orcid": "0000-...", "role": "viewer" }

DELETE /resources/{id}/share/{orcid}
```

---

## Project Membership

`POST /projects/{proj_id}/users/{orcid}?role=contributor` — defaults to `contributor`.

Project lead gets `admin` role in the project group at creation.

| Role | Can do |
|---|---|
| `viewer` | Read project resources |
| `contributor` | Create and manage their own resources |
| `editor` | Read and write all project resources |
| `admin` | Full control, manage membership and ACLs |

Routes:
- `POST /projects/{proj_id}/users/{orcid}?role=` — add member
- `PATCH /projects/{proj_id}/users/{orcid}?role=` — change role
- `GET /projects/{proj_id}/users` — list members with roles
- `DELETE /projects/{proj_id}/users/{orcid}` — remove member

---

## Ownership Transfer

> **Deferred.**

When an owner leaves, their resources remain under their ORCID group. Only the owner can modify that ACL entry; platform admins have no route to reassign it.

Open questions:
- Who can initiate a transfer — platform admins only, or also project admins?
- Does the previous owner retain a lower role after transfer?
- Can ownership transfer to a project group rather than an individual?

---

## Instrument Service Accounts

> **Draft — to be revisited at implementation.**

Instrument service accounts are `User` records with `is_service_account=True`. They need broad read access and write access to associated projects, without platform admin rights.

### `service_accounts` group

Members get:
1. Read-only bypass — can read all resources without an ACL entry
2. Access to allowlisted routes normally restricted to admins (e.g. `POST /users/resolve`)

Write operations require explicit project association.

### Instrument group

Each instrument has a group named after its MFID. The instrument resource has that group in its ACL.

| Role | Who | Can do |
|---|---|---|
| `owner` | Instrument creator | Full management, decommission |
| `admin` | Beamline scientists, facility staff | Edit metadata, change status, manage project associations |
| `admin` | Instrument service account | Update instrument metadata (not status, not group membership) |

The service account holds `admin`, not `owner` — it cannot decommission itself or manage group membership.

### Instrument-project association

A project admin associates an instrument with a project. This auto-grants the service account `contributor` role in that project group. Removing the association revokes it.

```
POST   /instruments/{id}/projects/{proj_id}
DELETE /instruments/{id}/projects/{proj_id}
```

An instrument can be associated with multiple projects simultaneously.

### Dataset ownership

Datasets created by an instrument service account are owned by the researcher. The `owner_orcid` in the creation payload specifies the owner; the ORCID group ACL entry is created for that researcher, not the instrument.

### Instrument status

| Status | Write access |
|---|---|
| `active` | Yes, via project associations |
| `maintenance` | Yes, maintenance is temporary |
| `decommissioned` | No, all project associations auto-revoked |

`GET /instruments` defaults to `status=active`. Only instrument `owner`, `admin`, or platform admins can change status. On decommissioning, all project associations are removed and the instrument group membership is kept for audit.

---

## What Needs to Change

### Schema
- [ ] Add `role VARCHAR DEFAULT 'contributor'` to `useraccessgrouplink`
- [ ] Add `is_platform_admin BOOLEAN DEFAULT FALSE` to `user`
- [ ] Migration: set `is_platform_admin=True` for existing platform admin users
- [ ] Migration: set ORCID-named group memberships to `owner`
- [ ] Migration: set project lead memberships to `admin`
- [ ] Migration: set `admin` group memberships to `admin`
- [ ] Drop `read`/`write` booleans from `resourceacl` after migration

### Models
- [ ] `UserAccessGroupLink` — add `role: str = 'contributor'`
- [ ] `RoleEnum`: `viewer`, `contributor`, `editor`, `admin`, `owner`

### Auth Utils
- [ ] Update `authorize_user` to return `(User, list[AccessGroup])` instead of `(orcid, list[AccessGroup])`
- [ ] Add `require_permission(user, resource_id, user_groups, permission)`
- [ ] Keep `require_admin` as an alias during migration
- [ ] Replace `require_read_access` and `require_write_access` route by route

### Resource Creation
- [ ] `create_standard_acls` — add owner ORCID entry with `owner` role, project entry

### Resource Sharing
- [ ] `POST /resources/{id}/share` — owner or project admin only
- [ ] `DELETE /resources/{id}/share/{orcid}` — owner or project admin only

### Project Routes
- [ ] `POST /projects/{proj_id}/users/{orcid}` — add `role` param
- [ ] `PATCH /projects/{proj_id}/users/{orcid}` — new route to update role
- [ ] `GET /projects/{proj_id}/users` — include role in response
- [ ] Project creation — assign lead `admin` role in project group

### Deletion
- [ ] Replace global-admin deletion with `require_permission(..., 'delete')`
- [ ] Resource owners and project admins can delete within their scope

### Instruments
- [ ] Add `status VARCHAR DEFAULT 'active'` to `instrument` table
- [ ] `GET /instruments` — default to `status=active`, expose `?status=` param
- [ ] `PATCH /instruments/{id}` — status change requires instrument `admin`/`owner` or platform admin
- [ ] `POST /instruments/{id}/projects/{proj_id}` — associate, auto-grant `contributor`
- [ ] `DELETE /instruments/{id}/projects/{proj_id}` — disassociate, auto-revoke
- [ ] At instrument creation, create instrument group; add creator as `owner`, service account as `admin`

### Access Group Routes
- [ ] Expose role in `GET /access_groups/{name}/users`
- [ ] Allow role updates by group admins

### Data Model Integrity
- [ ] Add `is_personal BOOLEAN DEFAULT FALSE` to `project` table
- [ ] Create `resource_project_link` table
- [ ] Add FK: `Dataset.owner_orcid` → `user.orcid`
- [ ] Add FK: `Sample.owner_orcid` → `user.orcid`
- [ ] Remove `project_id` from `Dataset` and `Sample` after migration
- [ ] At user registration, auto-create personal project (`project_id = orcid`, `title = "{first_name}'s workspace"`)

---

## Migration Path

1. Add `role VARCHAR DEFAULT 'contributor'` to `useraccessgrouplink`
2. Backfill roles from current `resourceacl` booleans:
   - `read=true, write=false` → `viewer`
   - `read=true, write=true` → `contributor`
   - ORCID-named group memberships → `owner`
   - Project lead memberships → `admin`
3. Implement `require_permission` alongside existing checks — no routes changed yet
4. Migrate routes one by one
5. Drop `read`/`write` booleans from `resourceacl`

Existing users with `write=true` are mapped to `contributor`, not `editor`. Project leads can promote users on a case-by-case basis.
