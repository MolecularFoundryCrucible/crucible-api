# Skipped Routes

Routes discussed and deferred. Revisit when a concrete use case arises.

## `GET /access_groups`

List all access groups. Deferred because there is no immediate use case. Identity, project, instrument, custom, and system groups share this table, so exposing a full list has privacy and internal-topology implications that require an explicit response and authorization policy.

## `DELETE /access_groups/{name}`

Delete an access group. Deferred because it requires deciding whether to cascade-delete linked ACLs and user memberships or reject if the group is in use. Entity-owned and system groups must never be removable through a generic custom-group operation.

## `DELETE /users/{orcid}`

User deactivation or removal is designed in [user_deactivation.md](user_deactivation.md).

## Generalize `AssociatedFile.dataset_id` to a resource-generic `resource_id`

Would let samples/instruments attach files with no new table or route family, matching the pattern `ResourceACL`/`ScientificMetadataBinary`/`DeletionRequest` already use. No current use case, and it's a real migration, not a rename. See "Future: generalizing file attachment" in `docs/remote_file_records.md` for the full writeup.
