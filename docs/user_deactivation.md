# User deactivation

Status: Deferred. Recorded 2026-10-06.

This document describes a proposed design. It is not evidence that a route, schema field, or migration exists. Confirm current behavior in OpenAPI and the implementation before building a client against this proposal.

## Motivation

Administrators sometimes need to stop a person from using the API without touching their data. The user owns samples, datasets, and projects, and appears in audit and deletion histories, so deletion is not an option. Deactivation is the middle ground: the identity keeps its ownership and history, but can no longer authenticate.

## Why not deletion

Permanent deletion of a User row would orphan the resources the person owns, break foreign keys from `ApiToken`, `UserAccessGroupLink`, and `UserProjectLink`, and erase the identity from `DeletionAuditLog` context. Ownership transfer covers individual resources but not a whole account, and audit trails must keep naming the person who performed an action. Deactivation keeps every relationship intact.

## Proposed design

- Add a nullable `is_active` Boolean column to the `user` table. It requires a matching Alembic revision. Null and `true` both mean active, so the migration changes no behavior and existing rows need no backfill.
- Check the flag when building a Principal: `load_principal` and the Hub sign-in path reject deactivated users with the same 401 shape as an unknown user, so the response cannot be used to probe account state. Deactivated users' API keys and Hub sessions stop working at their next authentication; the in-process principal cache may retain access for up to `AUTH_CACHE_TTL` (default 60 seconds) after deactivation.
- Deactivated users keep ownership and history. Their resources, group memberships, ACL grants, and audit records are unchanged. Resources they own remain accessible to other authorized callers.
- Add an administrator-only route like `PATCH /users/{user_id}/active` with a Boolean body, following the guard shape of `PATCH /users/{user_id}/platform_role`: human platform administrators only, service accounts cannot call it, self-deactivation is rejected, and unknown users return 404. The response returns the administrator-visible user representation.
- Interaction with the last-admin guard: deactivating a human platform administrator runs the same check as demoting one. It is rejected with 409 when no other human user holds `platform_role=admin`, evaluated with the target row locked inside the transaction.
- Reactivation restores authentication immediately. No data is restored or recreated, because none was removed.
- Cache invalidation: deactivation should invalidate the target's API keys in the auth cache the way `DELETE /users/{user_id}/apikey` does, and document the residual TTL window. Hub access tokens are short-lived and are rejected once `load_principal` refuses the user.
