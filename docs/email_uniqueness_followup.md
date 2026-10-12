# User email uniqueness

Status: Implemented 2026-10-12 (migration `e5a1c8d93f27`). Requirement recorded 2026-09-28.

## Behavior

Email is optional and unverified, and it is used as a human-friendly identifier: `{user_ref}` path segments, the deprecated `email=` membership parameter, and project-lead fields resolve users by case-insensitive email. Because of that, a non-null email belongs to at most one user, including service accounts.

- Writes trim surrounding whitespace, lowercase the address, and store blank values as null (`UserCreate`, `UserUpdate`, `AccountProfileUpdate`).
- `POST /users`, `PATCH /users/{user_ref}`, and `PATCH /account/profile` return 409 with `{"detail": {"code": "already_in_use", "fields": ["email"], "message": "Email is already in use"}}` when another user holds the address, without identifying that user. A username conflict in the same request is listed in the same `fields` array. A concurrent conflicting write is rejected by the database index and also returns 409.
- The partial unique index `ux_user_email_lower` on `lower(email)` allows any number of users without an email.
- Hub sign-in identifies users by ORCID; matching email addresses never merge or link accounts. Uniqueness does not prove ownership of an address: the first account to set it keeps it until changed.

## Rollout

1. Run `migrations/data/20261012_audit_user_emails.py` (read-only by default) against the target database. It reports shared addresses by user identifier only, plus blank and whitespace-padded values.
2. Resolve every shared address by hand: decide which account keeps it and clear or change the others. Never merge accounts automatically. `--commit` only nulls blank values and trims whitespace.
3. Apply migration `e5a1c8d93f27`. It refuses to run while shared addresses remain and builds the index concurrently.

The 2026-10-12 audit found one shared address (accounts `0000-0002-2788-194X` and `0000-0003-2692-1935`) and one blank value, identical on staging and production.
