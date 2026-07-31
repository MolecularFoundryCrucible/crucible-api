# Skipped Routes

Routes discussed and deferred. Revisit when a concrete use case arises.

## `GET /access_groups`

List all access groups. Deferred — no immediate use case. Group names mirror project IDs and user ORCIDs, so exposing a full list has privacy implications worth thinking through before adding.

## `DELETE /access_groups/{name}`

Delete an access group. Deferred — requires deciding whether to cascade-delete linked ACLs and user memberships or reject if group is in use. No immediate use case.

## `GET /resources/{id}/access_groups` and `POST /resources/{id}/access_groups`

Resource-level ACL inspection and assignment (generalizing the existing dataset-specific routes to samples and instruments). Deferred — no immediate use case beyond datasets.

## `DELETE /users/{orcid}`

User deactivation or removal. Deferred — requires a design decision on soft vs. hard delete. Hard delete must cascade `ApiToken`, `UserAccessGroupLink`, `UserProjectLink`. Also needs a policy for resources where the deleted user is stored as `owner_orcid` on datasets.

## Generalize `AssociatedFile.dataset_id` to a resource-generic `resource_id`

Would let samples/instruments attach files with no new table or route family, matching the pattern `ResourceACL`/`ScientificMetadataBinary`/`DeletionRequest` already use. No current use case, and it's a real migration, not a rename. See "Future: generalizing file attachment" in `docs/remote_file_records.md` for the full writeup.
