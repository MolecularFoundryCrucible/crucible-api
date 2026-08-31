# User creation reliability migration

## Status

Deferred. This document records the agreed repair for `POST /users`. The current route behavior remains unchanged.

## Current defects

The route combines explicitly requested project IDs with proposal-database results using `project_ids.append(user_proposal_ids)`. This creates a nested list instead of one list of project IDs. The nested list is later compared with `Project.project_id`, which can fail in SQLAlchemy or the database driver. An empty proposal result still appends `[]`, so the defect is not limited to users with proposals.

The proposal-service request also runs while the database transaction and connection are open. It has no timeout, parses JSON before checking the HTTP status, parses the same response repeatedly, and can hold database resources for the full duration of a slow upstream request.

Project records are then queried one at a time, duplicate identifiers are not removed, and unknown projects are only logged and skipped.

## Agreed behavior

`POST /users` remains platform-admin-only and retains its current request and response models. Automatically discovered and explicitly requested project memberships retain their current contributor standing.

The corrected flow should:

1. Validate the request before performing external or database work.
2. Query the proposal service before opening a database transaction.
3. Use a bounded HTTP timeout.
4. Check the response status before parsing JSON and parse the response only once.
5. Return `502 Bad Gateway` if the proposal service is unavailable, times out, returns an unsuccessful response, or returns invalid data.
6. Combine explicit and discovered project IDs with a deduplicated union.
7. Fetch all matching projects in one `IN (...)` query.
8. Reject unknown explicitly requested project IDs rather than reporting a partially successful request.
9. Log and ignore discovered proposal IDs that do not yet have a local project because proposal and Crucible project provisioning may not occur at the same time.
10. Create or update the user, identity group, project relationships, and project access-group memberships in one transaction.

The route should not create a user without proposal memberships when the proposal lookup fails. Returning success for an incomplete account would make the failure difficult to detect and repair. A `502` response leaves the operation retryable.

## Database efficiency

The implementation should avoid one project lookup per identifier and should not call the generic membership helper repeatedly if that produces multiple user, group, and membership queries for every project. Load the user, projects, project access groups, and existing membership links in bounded set-based queries, then insert only missing relationships.

## Required tests

- Proposal IDs are flattened and deduplicated with explicit IDs.
- An empty proposal result does not create a nested project identifier.
- Proposal timeout, unsuccessful status, and invalid JSON return `502` before any database write.
- Unknown explicit projects reject the request.
- Unknown discovered proposals are ignored and logged.
- Project lookup and membership loading remain batched as the number of projects increases.
- User, identity group, project relationships, and access-group memberships commit atomically.
