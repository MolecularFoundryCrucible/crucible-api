# Join requests and notifications design

Status: approved (2026-10-16). Phase 1 (join requests) is being built. In-app notifications are a possible future direction, not scheduled. Email notifications are out of scope.

## Goals

- A researcher can ask to join a project, see where the request stands, and withdraw it.
- Anyone who can add members to a project can find the requests waiting for them, see who is asking, and approve at a chosen role or reject, in one place across all their projects.
- Lists are paginated, sortable, filterable, and countable (facets), with the same conventions as the rest of the API.
- Reviewers can see how many requests wait for them (facet counts) without opening each project.

## Current state and problems

Routes: `POST /access_groups/{group_name}/join`, `GET /join_requests`, `GET /join_requests/{id}`, `PATCH /join_requests/{id}`, `GET /account/join_requests`. The only client is the nano-crucible CLI.

| Area | Problem |
|---|---|
| Correctness | `GET /account/join_requests` returns 500 for anyone with a request (`group_name` is required by the response model but never set). Lists return 500 when a request's project row is gone. |
| Reviewers | Only project owners and platform admins can see or review requests, while editors and above can add members directly. |
| Approval | Always grants contributor; the reviewer cannot choose the role. |
| Lifecycle | Requesters cannot withdraw. A request stays pending after the person is added to the project another way. |
| Naming | The project slug is called `group_name`; creation lives under `/access_groups` though only projects can be joined. |
| Data | `status` is free text without a constraint; `request_time` and `review_time` are text, not timestamps. |
| Listing | Offset pagination only, no sort, filters limited to status and requester, ORCIDs without names, no counts. |

## Phase 1: join requests

### Lifecycle

```
pending --approve--> approved
pending --reject---> rejected
pending --withdraw-> withdrawn
pending --member added another way--> approved (closed automatically)
```

Only `pending` requests change. Any transition from another state returns 409 `join_request_closed` with the current status. A requester may have one pending request per project (existing partial unique index); after a request closes they may ask again. A requester may have at most 20 pending requests in total (409 `too_many_pending_requests`), to keep the feature from being used to spam project leads.

### Who may do what

- **Request:** any signed-in user who is not a member of the project, while the project is not pending deletion (409 `project_not_accepting_requests`). Service accounts cannot request (403); they are added by people.
- **Review (approve or reject):** platform admins, and anyone holding editor or above in the project, the same people who can add members directly. Service accounts cannot review unless they act with platform-admin authority, as today.
- **Granted role:** the reviewer chooses `viewer`, `contributor` (default), `editor`, or `admin`, strictly below their own role in the project, the same rule as direct adds (editors grant viewer or contributor, admins up to editor, owners up to admin). `owner` is never granted this way (ownership moves only through transfer). A role at or above the reviewer's returns 403 with the same message as direct adds. Platform admins may grant up to admin.
- **Withdraw:** the requester only.
- **Read:** the requester, anyone who may review it, and platform admins. Everyone else gets 404, so request IDs do not reveal who asked to join what.

### Routes

| Route | Purpose |
|---|---|
| `POST /projects/{project_ref}/join_requests` | Create. Body `{reason?}` (at most 2000 characters). 201 with the request. 409 `already_member`, `join_request_pending`, `too_many_pending_requests`; 404 unknown project. |
| `GET /join_requests` | List (below). |
| `GET /join_requests/facets` | Counts (below). |
| `GET /join_requests/{request_id}` | Read one. |
| `PATCH /join_requests/{request_id}` | Review. Body `{status: "approved" \| "rejected", role?, reviewer_notes?}` (notes at most 2000 characters). `role` only with `approved`. Returns the updated request. |
| `POST /join_requests/{request_id}/withdraw` | Requester withdraws a pending request. `review_time` records when it closed. |

Deprecated, kept as wrappers with the new behavior and response:

- `POST /access_groups/{group_name}/join` -> `POST /projects/{project_ref}/join_requests` (returns 200 as before).
- `GET /account/join_requests` -> `GET /join_requests?scope=submitted`.

### Listing

`GET /join_requests` parameters:

- `scope`: `accessible` (default; your own requests plus those you may review), `submitted` (your own), `reviewable` (those you may review, any status).
- `status`: repeatable (`?status=pending&status=approved`).
- `project_mfid` (repeatable), `requester_id`, `reviewer_id`.
- `requested_after`, `requested_before`, `reviewed_after`, `reviewed_before`: timezone-aware timestamps.
- `q`: matches the requester's name, username, or ORCID (case-insensitive).
- `sort`: `request_time` (default), `review_time`, `status`, `project`, `requester_name`; `direction` `asc` or `desc` (default `desc`). Text sorts are case-insensitive, nulls last, request id is the final tie-breaker; status order is pending, approved, rejected, withdrawn.
- `limit` (1 to 1000, default 100, as before), `cursor` (signed keyset cursor bound to the scope, filters, sort, and caller, as elsewhere), `offset` kept for compatibility.

Response: `PaginatedResponse[JoinRequestRead]` with `next_cursor`.

`GET /join_requests/facets?field=status|project|requester` takes the same scope and filters and returns `FacetResponse` (project buckets carry the project reference, requester buckets the public user), ordered by count and then label, at most `limit` buckets (1 to 200, default 50). It gives the counts behind a "3 pending" badge and the project filter in a review queue.

### Response item

```json
{
  "id": 42,
  "status": "pending",
  "project_mfid": "0t...",
  "project": {"unique_id": "0t...", "project_id": "thin-films", "title": "Thin films"},
  "requester": {"unique_id": "0000-...", "username": "ehwong", "first_name": "...", "last_name": "..."},
  "reason": "Joining the XRD campaign",
  "request_time": "2026-10-16T17:02:11Z",
  "reviewer": null,
  "review_time": null,
  "reviewer_notes": null,
  "granted_role": null,
  "capabilities": {"can_review": true, "can_withdraw": false},
  "group_name": "thin-films"
}
```

`group_name` stays as a deprecated alias of `project.project_id`. `requester` and `reviewer` use the public user shape, so no emails are exposed. When the project row is gone, `project` is null and `project_mfid` still names it.

### Automatic closing

Whenever a user is added to a project (direct add, approval of another request, ownership transfer), any pending request of theirs for that project is closed as `approved` with `reviewer` set to the actor, `granted_role` set to the role they received, and `reviewer_notes` "Added to the project directly". This lives in the membership helper so every route gets it.

### Data changes (one migration)

- `request_time`, `review_time`: text to `timestamptz` (existing values are ISO 8601 and convert with `::timestamptz`; production had no rows on 2026-10-16). Missing request times are set to the migration time.
- `status`: CHECK in (`pending`, `approved`, `rejected`, `withdrawn`).
- New `granted_role` (`resource_role` enum, nullable).
- Index `(accessgroup_id, status, request_time)` for review queues; the existing per-requester index serves `submitted`.
- Length CHECKs on `reason` and `reviewer_notes` (2000).

### Tests

Lifecycle transitions and 409s; reviewer eligibility by role (viewer and contributor cannot review, editor can, service account cannot); role caps (editor grants contributor but not editor, owner grants admin, nobody grants owner); requests to projects pending deletion rejected; withdraw by requester only; 404 for unrelated callers; scopes, each filter, each sort with cursor paging across ties; facets; automatic closing from a direct add; deprecated routes; the 500s that exist today.

## Future: in-app notifications

Not scheduled. Recorded so a later design starts from the same principles; phase 1 does not depend on it.

### Model

One row per recipient and event (fan-out on write; the user base is small and reads stay a single indexed query).

| Column | Meaning |
|---|---|
| `id` | Primary key. |
| `user_id` | Recipient (`user.id`, cascade). |
| `type` | Typed event: `join_request.created`, `join_request.reviewed`, later `deletion_request.*`, `postprocessing_run.finished`. |
| `subject_type`, `subject_id` | What it is about (`join_request`, 42). |
| `actor_id` | Who caused it, nullable. |
| `thread_key` | Collapse key, for example `join_request:42`. A newer event on the same thread updates the existing unread row instead of adding another. |
| `created_at`, `updated_at`, `read_at` | Timestamps; unread means `read_at` is null. |

Rows are written in the same transaction as the change that causes them, so a rolled-back approval never notifies. They are not snapshots: the API renders each notification from the current subject at read time and hides ones whose subject the recipient can no longer read. Rows older than 90 days are deleted by the existing cleanup job pattern.

### Routes

- `GET /account/notifications`: cursor-paginated, newest first, `unread=true` filter, `type` filter. Supports `If-None-Match` with an ETag derived from the newest `updated_at` and unread count, so polling is cheap and returns 304 when nothing changed.
- `GET /account/notifications/summary`: `{unread: n, by_type: {...}}` for badges.
- `POST /account/notifications/read`: body `{ids: [...]}` or `{before: <timestamp>}` (mark all up to a point, which is safe against a notification arriving between the read and the click).
- Per-type mute lives in preferences under a typed `notifications` key, not in the notification table.

### Delivery

Polling with ETags first (the Hub already polls). Server-sent events can be added later behind the same summary endpoint without changing the model.

## Decisions

- Approvals and direct adds grant the same roles: strictly below the reviewer's own role.
- Projects pending deletion accept no new requests; their existing requests stay as they are. Archived projects should do the same: archiving does not exist yet, and `project_archiving.md` (which currently lets archived projects keep accepting requests) is updated to match. Deleting a project removes its join requests with its access group (already the case, reported as `join_requests_removed`).
- Email notifications are out of scope.

## Rollout

1. Phase 1 on staging with the migration; nano-crucible moves to the new routes and shows names, roles, and withdraw; the Hub can add a review queue using facets.
2. Deprecated routes stay for at least one minor release, then are removed with a changelog entry.
