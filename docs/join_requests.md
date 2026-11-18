# Join requests

This page covers how users request to join a project and how project editors review those requests. Project roles are defined in [Permissions](permissions.md#projects). The API sends no notifications: reviewers find pending requests by listing them.

## Lifecycle

```
pending --approve--> approved
pending --reject---> rejected
pending --withdraw-> withdrawn
pending --member added another way--> approved (closed automatically)
```

Only `pending` requests change; any transition from another status returns 409 `join_request_closed` with the current `status`. A requester may have one pending request per project and at most 20 pending requests in total. After a request closes, the requester may submit a new one.

## Authorization

- **Request:** any authenticated user who is not a member of the project. Service accounts cannot request (403 `service_account_cannot_request`); a project editor adds them directly.
- **Review:** users holding `editor` or above in the project, the same users who can add members directly, and platform administrators. A service account reviews only with platform-administrator authority.
- **Granted role:** the reviewer chooses `viewer`, `contributor` (default), `editor` or `admin`, at most the reviewer's own project role, the same rule as direct adds. `owner` is never granted this way; ownership changes only through transfer. A role above the reviewer's returns 403. Platform administrators may grant up to `admin`.
- **Withdraw:** the requester only (403 `withdraw_not_allowed` for others who can read the request).
- **Read:** the requester, users who may review the request, and platform administrators. All other callers receive 404 `join_request_not_found`, so request IDs do not reveal who asked to join which project.

"Platform administrator" means an administrator in elevated mode ([explicit privilege mode](explicit_privilege_mode.md)).

## Routes

| Route | Description |
|---|---|
| `POST /projects/{project_ref}/join_requests` | Create. Body `{"reason": ...}`, optional, at most 2000 characters. Returns 201 with the request. |
| `GET /join_requests` | List (see [Listing](#listing)). |
| `GET /join_requests/facets` | Counts (see [Listing](#listing)). |
| `GET /join_requests/{request_id}` | Read one. |
| `PATCH /join_requests/{request_id}` | Review. Body `{"status": "approved" \| "rejected", "role": ..., "reviewer_notes": ...}`; notes at most 2000 characters, `role` only with `approved`. Returns the updated request. |
| `POST /join_requests/{request_id}/withdraw` | Withdraw a pending request. `review_time` records when it closed. |

Errors on creation: 404 `project_not_found`; 409 `already_member`, `join_request_pending` (with the existing `request_id`), `too_many_pending_requests`, or `project_not_accepting_requests` when the project is pending deletion. Errors on review: 403 `review_not_allowed`; 409 `join_request_closed`, `project_not_accepting_requests`, or `join_request_unresolvable` when approving a request whose project or requester no longer exists.

Projects pending deletion accept no new requests and no approvals; rejection and withdrawal remain possible. Deleting a project deletes its join requests, reported as `join_requests_removed` in the deletion preview.

## Listing

`GET /join_requests` parameters:

- `scope`: `accessible` (default; the caller's own requests and those the caller may review), `submitted` (the caller's own), `reviewable` (those the caller may review, any status). Platform administrators see every request under `accessible`.
- `status`: repeatable (`?status=pending&status=approved`).
- `project_mfid` (repeatable), `requester_id`, `reviewer_id`.
- `requested_after`, `requested_before`, `reviewed_after`, `reviewed_before`: timezone-aware timestamps. The review times also cover withdrawal and automatic closing.
- `q`: matches the requester's name, username or ORCID, case-insensitively.
- `sort`: `request_time` (default), `review_time`, `status`, `project`, `requester_name`; `direction` `asc` or `desc` (default `desc`). Text sorts are case-insensitive, nulls sort last, and the request ID breaks ties. Status order is pending, approved, rejected, withdrawn.
- `limit` (1 to 1000, default 100); `cursor`, the signed `next_cursor` of the previous page, bound to the scope, filters, sort and caller; `offset` for compatibility, ignored when `cursor` is given.

The response is `PaginatedResponse[JoinRequestRead]` with `next_cursor`.

`GET /join_requests/facets?field=status|project|requester` takes the same scope and filters and returns a `FacetResponse`: project buckets carry the project reference, requester buckets the public user. Buckets are ordered by count, then label, with at most `limit` buckets (1 to 200, default 50). The facets provide counts such as the number of pending requests per project for a review queue.

## Response item

```json
{
  "id": 42,
  "status": "pending",
  "project_mfid": "0t...",
  "project": {"unique_id": "0t...", "project_id": "thin-films", "title": "Thin films"},
  "requester_id": "0000-...",
  "requester": {"unique_id": "0000-...", "username": "ehwong", "first_name": "...", "last_name": "..."},
  "reason": "Joining the XRD campaign",
  "request_time": "2026-10-16T17:02:11Z",
  "reviewer_id": null,
  "reviewer": null,
  "review_time": null,
  "reviewer_notes": null,
  "granted_role": null,
  "capabilities": {"can_review": true, "can_withdraw": false},
  "group_name": "thin-films"
}
```

`requester` and `reviewer` use the public user representation, which contains no email address. When the project no longer exists, `project` is null and `project_mfid` still identifies it. `capabilities` are false on closed requests.

## Automatic closing

When a user is added to a project by any other route (direct add, proposal sync, ownership transfer), their pending request for that project is closed as `approved`, with `granted_role` set to the role they received and `reviewer_notes` "Added to the project directly". `reviewer` is the user who added them, and null for proposal sync and ownership transfer.

## Deprecated routes and fields

These remain served in 4.0.0 with the behavior and response described above:

- `POST /access_groups/{group_name}/join`, where `group_name` is a project slug or MFID: use `POST /projects/{project_ref}/join_requests`. Returns 200 instead of 201.
- `GET /account/join_requests` (parameters `status`, `limit`, `offset`): use `GET /join_requests?scope=submitted`.
- The `group_name` query parameter of `GET /join_requests` and `GET /join_requests/facets`, a project slug or MFID: use `project_mfid`.
- The `group_name` response field, the project slug (or the MFID when the project no longer exists): use `project.project_id`.
