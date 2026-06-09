# Permission System Redesign

## Goals

The current permission model is binary: read or write access to a group's resources. This is too coarse for a platform where researchers, collaborators, facility staff, instrument software, and operators all have different levels of responsibility.

The redesign adds five roles (`viewer`, `contributor`, `editor`, `admin`, `owner`) applied at the group level. Permissions are scoped by context: your role in a project group governs what you can do to project resources; your role in your ORCID group covers your own resources; platform admin access is a separate bypass outside the ACL system.

---

## Core Concepts

### Resources

In Crucible, a resource is anything with an MFID: datasets, samples, instruments, and projects. All resource types share the same permission system.

Every resource has:
- An **owner**: the user whose ORCID group has an ACL entry with `owner` role
- **ACL entries** in `resourceacl`: which groups can access the resource and at what role

Projects and instruments are resources in their own right — they have owners and ACL entries, and go through the same permission checks as datasets and samples.

### Roles

To determine a user's effective role on a resource:

1. Find all access groups the user belongs to that also have an ACL entry on that resource
2. Check the user's role in each of those groups (`useraccessgrouplink`)
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
- **editor**: can read and write all resources in the group regardless of who created them
- **admin**: full control within the group; can manage ACL entries at or below admin level
- **owner**: the highest non-platform role. Automatically assigned in a user's ORCID group at resource creation. The permission check treats it identically to any other role — if your ORCID group has an ACL entry on this resource and your role in that group is `owner`, you have owner-level rights.

ACL management follows the same hierarchy: a user can only manage entries at or below their own effective role. `owner` can manage any entry except the platform admin bypass (immutable). `admin` can manage entries for `editor`, `contributor`, `viewer` but cannot touch the owner's ORCID group entry. `editor` and below cannot manage ACL entries.

### Groups

Roles are assigned via group membership. Five types of groups participate in the system:

| Group | Named after | Members | Role in group |
|---|---|---|---|
| Project group | `project_id` | Project members | their project role |
| ORCID group | user's ORCID | The user | `owner` |
| `admin` group | `admin` | Platform admins | `admin` |
| `service_accounts` group | `service_accounts` | Instrument service accounts | `admin` |
| Instrument group | instrument MFID | Service account (`admin`), human admins (`admin`/`owner`) | see Instruments |

#### ORCID groups and ownership

Every user has a personal group named after their ORCID. At resource creation, this group is added to the resource's ACL with the `owner` role. There is no separate ownership concept — ownership is a consequence of having `owner` role in a group that has an ACL entry on the resource.

---

## Platform Access

### Platform admins

Platform admins bypass all resource-level permission checks. This is stored as a flag on `User`, not as group membership — groups control resource-level access, user flags control system-level bypass.

```sql
ALTER TABLE "user" ADD COLUMN is_platform_admin BOOLEAN DEFAULT FALSE;
```

`authorize_user` is updated to return the `User` object alongside the group list:

```python
def authorize_user(credentials) -> tuple[User, list[AccessGroup]]:
    return user, ag_list
```

The permission check reads directly from the user object (no extra query):

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

When this user makes a request, `require_permission` checks `user.is_platform_admin = True` and returns immediately. `resourceacl` is never consulted.

The `admin` group still exists as an organizational container and for route-level checks (e.g. `require_admin`), but it no longer drives the permission bypass.

### Service accounts

A service account is a `User` record with `is_service_account=True`. This flag drives two behaviors:

1. **Read-only bypass** — can read all resources without an ACL entry; no need to be added to every project
2. **Elevated route allowlist** — access to routes normally restricted to admins but needed for automation (e.g. `POST /users/resolve`)

Write access is never bypassed — it requires explicit project group membership.

Service accounts cannot manage ACL entries regardless of their group role. This is enforced in code.

The `service_accounts` group exists as an organizational container. The bypass comes from the `is_service_account` flag on `User`, not from group membership.

---

## Projects

### Project as a resource

A project has an MFID (its `project_id`) and lives in the `resource` table. It has an owner (the project lead) and ACL entries controlling who can view or edit the project's metadata.

Each project also has a **project group** (named after the `project_id`) that controls access to resources *within* the project. These are distinct:

- ACL entry on the project resource → who can view/edit the project itself
- Project group membership → who has access to the project's datasets and samples

### Personal projects

Every user gets a personal project created at registration. It uses their ORCID group as its access group.

- `Project.is_personal = True` distinguishes personal from formal projects
- Not shown in `GET /projects` for other users
- `project_id` is the user's ORCID — stable, never shown to users directly
- `title` (e.g. "Fabrice's workspace") is what the UI displays
- Cannot be deleted

### Multi-project resources

> **To be implemented** — `resource_project_link` does not exist yet. Currently `Dataset` and `Sample` each have a plain `project_id` string column.

A resource can belong to more than one project via a relationship table:

```sql
CREATE TABLE resource_project_link (
    resource_mfid VARCHAR REFERENCES idtype(mfid),
    project_id    VARCHAR REFERENCES project(project_id),
    PRIMARY KEY (resource_mfid, project_id)
);
```

At resource creation:
1. Owner's ORCID group is added to `resourceacl` with `owner` role
2. The creator's personal project is always linked
3. Any additional projects specified are also linked; their project group gets an ACL entry

Filtering by project becomes a join. Your personal workspace always reflects everything you created.

### Project membership

`POST /projects/{proj_id}/users/{orcid}?role=contributor` — defaults to `contributor`. Project lead gets `admin` at creation.

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

### Sharing

Primary mechanism: project groups. Adding a user to a project gives them access to all project resources at their role level.

Per-resource sharing is available as an escape hatch: an owner or project admin can share a single resource with someone outside the project. This adds one ACL entry pointing to the recipient's ORCID group.

```
POST /resources/{id}/share
Body: { "orcid": "0000-...", "role": "viewer" }

DELETE /resources/{id}/share/{orcid}
```

---

## Instruments

> **Draft — to be revisited at implementation.**

### Instrument as a resource

An instrument is a first-class resource: it has an MFID, an owner (the registrar), and ACL entries. Each instrument also has an **instrument group** (named after the instrument MFID) controlling who can manage it:

| Role | Who | Rights |
|---|---|---|
| `owner` | Instrument creator / registrar | Full management, decommission, transfer ownership |
| `admin` | Beamline scientists, facility staff | Edit metadata, change status, manage project associations |
| `admin` | Instrument service account | Update instrument metadata programmatically |

The service account holds `admin`, not `owner` — it cannot decommission the instrument or manage group membership.

### Instrument service account

An instrument service account is a `User` record (`is_service_account=True`) representing the software running on the physical instrument. It is an actor, not the instrument itself.

At instrument registration:
- A service account user is created with `is_service_account=True`
- Added to the `service_accounts` group (read bypass and route allowlist)
- Added to the instrument group with `admin` role (instrument metadata management)

### Status lifecycle

Instruments are not deleted. A `status` column tracks their lifecycle:

| Status | Service account write access |
|---|---|
| `active` | Yes, via project associations |
| `maintenance` | Yes, maintenance is temporary |
| `decommissioned` | No, all project associations auto-revoked |

`GET /instruments` defaults to `status=active`. Only instrument `owner`, `admin`, or platform admins can change status. On decommissioning, all project associations are removed; group membership is kept for audit.

### Project associations

A project admin associates an instrument with a project. This auto-grants the service account `contributor` role in that project group. Removing the association revokes it.

```
POST   /instruments/{id}/projects/{proj_id}
DELETE /instruments/{id}/projects/{proj_id}
```

An instrument can be associated with multiple projects simultaneously.

### Dataset ownership

Datasets created by an instrument service account belong to the researcher, not the instrument. The `owner_orcid` in the creation payload names the researcher; the ORCID group ACL entry is created for them.

---

## Deferred

### Ownership transfer

When an owner leaves, their resources remain under their ORCID group. Only the owner can modify that ACL entry; platform admins have no route to reassign it.

Open questions:
- Who can initiate a transfer — platform admins only, or also project admins?
- Does the previous owner retain a lower role after transfer?
- Can ownership transfer to a project group rather than an individual?

---

## Implementation

### Data model integrity

Currently `owner_orcid` and `project_id` on `Dataset` and `Sample` are plain strings with no FK enforcement. Changes required:

- `Dataset.owner_orcid` → FK to `user.orcid` (non-null)
- `Sample.owner_orcid` → FK to `user.orcid` (non-null)
- `resource_project_link.project_id` → FK to `project.project_id`
- `resource_project_link.resource_mfid` → FK to `idtype.mfid`

A resource cannot be created without a registered owner or assigned to a non-existent project. Deleting a user or project requires handling linked resources first.

### User resolution

The API always operates on ORCIDs internally. Usernames and emails are resolved at the client/UI layer. `POST /users/resolve` and `GET /users/by-username/{username}` serve this purpose.

### What needs to change

**Schema**
- [ ] Add `role VARCHAR DEFAULT 'contributor'` to `useraccessgrouplink`
- [ ] Add `is_platform_admin BOOLEAN DEFAULT FALSE` to `user`
- [ ] Drop `read`/`write` booleans from `resourceacl` after migration
- [ ] Add `is_personal BOOLEAN DEFAULT FALSE` to `project`
- [ ] Create `resource_project_link` table
- [ ] Add FK: `Dataset.owner_orcid` → `user.orcid`
- [ ] Add FK: `Sample.owner_orcid` → `user.orcid`
- [ ] Remove `project_id` from `Dataset` and `Sample` after migration
- [ ] Add `status VARCHAR DEFAULT 'active'` to `instrument` table

**Models**
- [ ] `UserAccessGroupLink` — add `role: str = 'contributor'`
- [ ] `RoleEnum`: `viewer`, `contributor`, `editor`, `admin`, `owner`

**Auth**
- [ ] Update `authorize_user` to return `(User, list[AccessGroup])`
- [ ] Add `require_permission(user, resource_id, user_groups, permission)`
- [ ] Keep `require_admin` as an alias during migration
- [ ] Replace `require_read_access` and `require_write_access` route by route

**Resource creation**
- [ ] `create_standard_acls` — owner ORCID entry with `owner` role, personal project link, project group ACL entry

**Sharing**
- [ ] `POST /resources/{id}/share` — owner or project admin only
- [ ] `DELETE /resources/{id}/share/{orcid}` — owner or project admin only

**Projects**
- [ ] `POST /projects/{proj_id}/users/{orcid}` — add `role` param
- [ ] `PATCH /projects/{proj_id}/users/{orcid}` — new route to update role
- [ ] `GET /projects/{proj_id}/users` — include role in response
- [ ] Project creation — assign lead `admin` role in project group
- [ ] At user registration, auto-create personal project (`project_id = orcid`, `title = "{first_name}'s workspace"`)

**Deletion**
- [ ] Replace global-admin deletion with `require_permission(..., 'delete')`
- [ ] Resource owners and project admins can delete within their scope

**Instruments**
- [ ] `GET /instruments` — default to `status=active`, expose `?status=` param
- [ ] `PATCH /instruments/{id}` — status change requires instrument `admin`/`owner` or platform admin
- [ ] `POST /instruments/{id}/projects/{proj_id}` — associate, auto-grant `contributor`
- [ ] `DELETE /instruments/{id}/projects/{proj_id}` — disassociate, auto-revoke
- [ ] At instrument creation, create instrument group; add creator as `owner`, service account as `admin`

**Access groups**
- [ ] Expose role in `GET /access_groups/{name}/users`
- [ ] Allow role updates by group admins

### Migration path

1. Add `role VARCHAR DEFAULT 'contributor'` to `useraccessgrouplink`
2. Backfill roles from current `resourceacl` booleans:
   - `read=true, write=false` → `viewer`
   - `read=true, write=true` → `contributor`
   - ORCID-named group memberships → `owner`
   - Project lead memberships → `admin`
3. Set `is_platform_admin=True` for existing platform admin users
4. Implement `require_permission` alongside existing checks — no routes changed yet
5. Migrate routes one by one
6. Drop `read`/`write` booleans from `resourceacl`

Existing users with `write=true` are mapped to `contributor`, not `editor`. Project leads can promote users on a case-by-case basis.
