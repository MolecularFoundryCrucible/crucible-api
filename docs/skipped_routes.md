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

Came up while designing remote (non-GCS) file records (see `docs/superpowers/specs/`). `AssociatedFile.dataset_id` is a hard `int` FK to `dataset.id`, unlike `ResourceACL.mfid`, `ScientificMetadataBinary.unique_id`, and `DeletionRequest.resource_id`, which all key off the generic `idtype.mfid` layer so they work across any resource type. Making `AssociatedFile` resource-generic the same way would let samples/instruments attach files with no new table or route family.

Deferred — no current use case (samples/instruments don't need files today), and it's a real migration (retype a live FK column + backfill from `dataset.unique_id`), not just a rename. Revisit if/when a non-dataset resource actually needs file attachments. Note: this trades a small amount of index efficiency (26-char mfid string keys vs. 4-byte int keys) for genericity — the same tradeoff already made for the three models above, and not expected to matter at Crucible's realistic scale.
