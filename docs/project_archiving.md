# Project archiving

Status: Deferred. Project archiving is useful for staging cleanup and normal project lifecycle management, but it is not required for the permission migration and would add an avoidable schema revision to the production rollout.

## Purpose

Archiving removes an inactive project from routine discovery without deleting its data or preventing later work. It is not a deletion mechanism and must not reuse the existing free-form `Project.status` field, which represents domain state rather than API lifecycle state.

## Proposed contract

Use an idempotent archive subresource:

- `PUT /projects/{project_ref}/archive` archives the project.
- `DELETE /projects/{project_ref}/archive` restores the project.

Both routes accept the same canonical MFID and compatibility project-reference forms as the project singleton route. Archiving requires project `admin`, project `owner`, or platform-administrator authority.

Avoid a generic status PATCH or an action-style `/unarchive` route. A dedicated subresource gives clients an explicit lifecycle operation and prevents accidental coupling to domain status values.

## Behavior

Archived projects:

- Are omitted from project collection, search, and picker results by default.
- Can be included explicitly with `include_archived=true`.
- Remain available through exact MFID or slug resolution.
- Continue to permit join requests, membership administration, uploads, derived sample and dataset creation, ACL administration, and ownership transfer.
- Retain all resources, relationships, and access rules.

Exact collection filters such as `GET /projects?project_id={slug}` should return an archived project because they are resolution requests rather than discovery requests. User-scoped project collections should follow the default omission rule and support the same `include_archived` option.

## Data model

Add nullable server-managed fields:

- `archived_at`: timestamp of the current archive operation. `NULL` means active.
- `archived_by_user_id`: internal reference to the identity that archived the project.

Public project responses should expose `archived: bool` and `archived_at`. They should not expose the internal archiving user ID. Unarchiving clears both stored fields. If historical archive events become important, add a lifecycle audit table rather than overloading these current-state fields.

## Implementation requirements

The future implementation requires an additive Alembic revision, indexes appropriate for the default active-project filters, permission and idempotency tests, OpenAPI regeneration, and client documentation. Apply the default filter consistently to `GET /projects`, project search, user-scoped project lists, and internal project picker helpers while exempting exact resolution.

The staging end-to-end harness may archive disposable projects after resource cleanup once this contract is implemented. Until then, it should use a persistent test project or leave created projects for case-by-case cleanup.
