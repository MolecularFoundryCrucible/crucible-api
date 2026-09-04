# Crucible Hub - Design Notes

Archived status: This early product design predates the completed permission overhaul and current instrument and service-account routes. Revalidate every API assumption before reusing any part of it.

A self-service web interface for Crucible users. Lets researchers manage their own data,
projects, and instruments without developer intervention. Built on top of the Crucible API;
the permission system overhaul (see `permission_system_plan.md`) defines the roles the hub
surfaces.

---

## Goals

- Every Crucible user can log in and manage their own data
- Project leads can manage membership and resources without filing tickets
- Instrument managers can register, configure, and associate instruments with projects
- Platform admins have a dedicated admin view - not mixed into the regular user experience
- The graph explorer remains a separate tool; the hub may absorb it later but should not
  be designed around that assumption

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Frontend | React + Vite | Fast builds, broad ecosystem, no framework lock-in |
| API state | TanStack Query | Cursor pagination, caching, background refetch, optimistic mutations |
| Styling | TailwindCSS | Utility-first, avoids component library lock-in |
| Auth | ORCID OAuth via Crucible API | Reuse existing flow; no second auth system |
| Backend | None (SPA direct to API) | No BFF - CORS configured on the API, Bearer token in memory |
| Deployment | Static build (Nginx / CDN) | Decoupled from API deployment |

No Flask BFF. The hub talks directly to the Crucible API. User resolution
(username → ORCID) happens client-side before API calls, using
`POST /users/resolve` or `GET /users/by-username/{username}`.

---

## Auth Flow

1. User visits hub → no token in memory → redirect to `GET /api/v3/auth/login`
2. ORCID OAuth dance → Crucible API returns API key via `/auth/apikey`
3. Hub receives API key, stores in React state (not localStorage - XSS risk)
4. All API calls attach `Authorization: Bearer <key>`
5. On token expiry or 401, redirect back to login

The hub never touches ORCID directly - the Crucible API is the OAuth client.

### API prerequisite
- CORS headers must be configured on the FastAPI app to allow the hub's origin
- The `/auth/apikey` response needs to be consumable by a SPA (currently returns
  HTML or JSON depending on the flow - worth reviewing for SPA compatibility)

---

## App Structure

```
/                       Personal workspace (own datasets, samples, files)
/projects               Projects the user belongs to
/projects/{id}          Project detail - resources, members, instruments
/projects/{id}/members  Member list with roles (post permission overhaul)
/instruments            Instruments the user manages
/instruments/{id}       Instrument detail - metadata, status, project associations
/admin                  Platform admin panel (platform admins only)
/admin/users            User list, flag management (is_platform_admin, is_service_account)
/admin/instruments      Instrument registration, service account provisioning
/profile                Own profile - username, email, API key management
```

---

## Views by Feature Area

### Personal Workspace (`/`)

The hub's entry point. Mirrors the personal project from the permission plan.

- Dataset list (cursor-paginated, filterable by keyword/date)
- Upload new dataset (file + metadata)
- Quick links to recent samples, projects
- Shows only resources owned by the user (project_id = user's ORCID personal project)

### Projects (`/projects`)

- List of projects the user is a member of
- Create new project (post permission overhaul: user becomes project admin)
- Per project: resource list, member list with roles, associated instruments

**Member management (project admin only):**
- Add member by username or ORCID → resolves to user → assign role
- Change role (contributor / editor / admin)
- Remove member

**Direct add is the primary mode** for now. A request-to-join flow can be layered on later
without restructuring this surface.

### Instruments (`/instruments`)

Available post instrument management API implementation.

- List instruments the user has admin/owner role on
- Register new instrument (platform admin or designated instrument registrar)
- Edit metadata, change status (active / maintenance / decommissioned)
- Associate/disassociate with projects (project admin must also approve)

### Admin Panel (`/admin`)

Separate section, not surfaced to regular users. Only rendered if
`user.is_platform_admin = true` in the API response.

- User management: search users, set `is_platform_admin`, `is_service_account`
- Instrument registration with service account provisioning
- Platform-wide deletion request review queue

---

## Phase Rollout

### Phase 1 - Personal workspace + project membership (shippable now)

Depends only on the current API. No permission overhaul needed.

- Login / logout
- Personal workspace: list, upload, view datasets and samples
- Project list and detail
- Add/remove project members by username or ORCID
- Profile page: view username, copy API key, ORCID info

**API gaps to close before Phase 1:**
- CORS on the API
- `/auth/apikey` flow reviewed for SPA compatibility
- `GET /projects/{proj_id}/users` should return roles (currently returns `UserRead` without role)

### Phase 2 - Role-aware UI (requires permission overhaul)

Once `useraccessgrouplink.role` exists and `authorize_user` returns the full `User` object:

- Project member list shows roles
- Add member defaults to `contributor`; project admins can set role
- `PATCH /projects/{proj_id}/users/{orcid}?role=` to change role
- Hub reads `user.is_platform_admin` to gate the admin panel
- Per-resource sharing surface: share a dataset with a specific user at a given role

### Phase 3 - Instrument management (requires instrument API)

Once `POST /instruments/{id}/projects/{proj_id}` and service account provisioning exist:

- Instrument registration flow (platform admin or registrar)
- Instrument detail and status management
- Project ↔ instrument association UI
- Instrument group member management (who has admin rights on the instrument)

### Phase 4 - Ownership transfer, join requests (deferred)

From the permission plan's "Deferred" section. Design later.

---

## API Changes Needed for the Hub

### Before Phase 1
- [ ] CORS middleware on FastAPI (`fastapi.middleware.cors.CORSMiddleware`)
- [ ] Review `/auth/apikey` response for SPA - ensure JSON path works without cookies
- [ ] `GET /projects/{proj_id}/users` - include `role` field per member
- [ ] User search: `GET /users?q=<name_or_username>` - fuzzy match on name/username,
      returns `[{orcid, username, first_name, last_name}]`, no email exposed

### Before Phase 2
- [ ] Everything in the permission overhaul implementation checklist
- [ ] `PATCH /projects/{proj_id}/users/{orcid}?role=` - new route
- [ ] `POST /resources/{id}/share` and `DELETE /resources/{id}/share/{orcid}`

### Before Phase 3
- [ ] `POST /instruments/{id}/projects/{proj_id}`
- [ ] `DELETE /instruments/{id}/projects/{proj_id}`
- [ ] `PATCH /instruments/{id}` - status change
- [ ] Service account provisioning endpoint

---

## Privacy Model in the Hub

| Field | Displayed | Searchable |
|---|---|---|
| Name | Yes | Yes (fuzzy) |
| Username | Yes | Yes (exact + fuzzy) |
| ORCID | Yes (on profile) | Yes (exact) |
| Email | Never | Accept as input only (not shown in results) |
| Projects | Own projects always; others' if project is public | No |
| Datasets | Governed by ACL | No |

User search returns name + username + ORCID only. Email is accepted as a resolution
input but never echoed back or shown in search results.

---

## Open Questions

- **Hub repository**: separate repo or monorepo alongside the API?
- **Hosting**: same GKE cluster as the API, or separate static hosting?
- **User search endpoint**: needed before Phase 1 ships - worth adding now alongside
  `POST /users/resolve`?
- **Personal project creation**: currently manual - should registration auto-create it?
  (The plan says yes; the API doesn't do it yet.)
- **Graph explorer convergence**: leave fully separate for now; revisit after Phase 2.
