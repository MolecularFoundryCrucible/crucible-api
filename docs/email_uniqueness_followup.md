# User email uniqueness

Status: Accepted requirement, implementation deferred. Recorded 2026-09-28.

## Current behavior

User email is nullable and has no unique constraint in the checked-in database model or migrations. User creation and updates do not reject duplicate emails. Some paths normalize email, but normalization is not uniqueness enforcement. Live database constraints and existing duplicate values have not been audited for this follow-up.

Hub sign-in identifies users by verified ORCID, not email. Matching email addresses must not automatically merge or link accounts.

## Required behavior

Non-null user email addresses must be unique across users, including service accounts. Keep email optional unless a separate requirement changes that policy. Use consistent whitespace trimming and case-insensitive matching across creation, updates, imports, and lookups. Do not infer provider-specific equivalence by removing dots or plus-address suffixes.

## Implementation checklist

1. Audit existing duplicate normalized emails, blank values, and live constraints without writing data. Resolve conflicts explicitly; do not silently merge accounts or overwrite addresses. Decide how existing blank strings become null.
2. Enforce uniqueness in PostgreSQL through a matching Alembic revision, allowing multiple null emails. An application availability check alone is insufficient under concurrent writes. Plan index creation and rollout for the live service.
3. Normalize all supported email writes consistently and return a documented HTTP 409 for conflicts, including concurrent database conflicts, without disclosing another account's identity.
4. Test creation, updates, case and whitespace variants, unchanged self-updates, null emails, service accounts, and concurrent conflicting writes. Keep ORCID-based sign-in unchanged.
5. Update OpenAPI descriptions, the changelog, and client guidance. Coordinate separately authorized Hub documentation and form-error handling updates. Rejecting duplicate writes changes previously accepted behavior and requires release communication.

No enforcement, migration, or data changes were made when recording this requirement.
