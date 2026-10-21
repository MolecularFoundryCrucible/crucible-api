# User representation and search: follow-up

Status: proposed 2026-10-21, not scheduled. Nothing here changes current behavior.

## One user schema with null fields

Today user routes return `Union[UserRead, UserPublicRead]`. Platform administrators and the user themselves get `UserRead` (with `email`, `platform_role`, `last_active_at`, `api_key_status`); everyone else gets `UserPublicRead`, which lacks those fields entirely. A `GET /users` page for a non-administrator mixes both shapes, with nothing in the payload saying which is which, so typed clients (the Hub's generated TypeScript) must narrow on the presence of `email`.

Proposal: one user schema whose private fields are always present and `null` when the caller may not see them, the convention `ProjectWithLead` already uses for `scientific_metadata` and `members`.

- Fields are always serialized, never omitted (FastAPI's default; do not add `exclude_none`).
- `null` must not be ambiguous. `email` and `api_key_status` can be legitimately empty, so clients read `user_capabilities.can_view_private` (and `can_manage_api_key` for key status) to tell "hidden" from "no value". `GET /users` returns `user_capabilities` on every item since 2026-10-21, which this relies on.
- Migration: the merged schema is a superset of `UserPublicRead`, so existing readers of public fields keep working. Coordinate with the Hub to regenerate types and drop shape narrowing.

## `/users/search` and `q` on `GET /users`

They serve different purposes and both stay for now:

- `GET /users/search` (June 2026) is a type-ahead for everyone: trigram match on names and username, ranked by relevance, at most 50 results, public profiles only.
- `q` on `GET /users` (October 2026) was added for the administrator directory: substring match that can include email and unique ID, combined with sorting, cursor pagination, and totals, which the search route cannot do.

Problem: `/users/search` returns a plain array while every other search route returns `PaginatedResponse`. The shape cannot change in place without breaking clients, including old nano-crucible releases installed from PyPI, so an opt-in flag whose default later flips is not safe either.

Proposal: add `sort=relevance` to `GET /users` (valid only with `q`; trigram ranking as in `/users/search`), keep the current `q` behavior when `sort` is omitted, mark `/users/search` deprecated with a `Deprecation` header, and remove it after the Hub and nano-crucible have moved and a minimum nano-crucible version is announced. Users then have one text-search path.

## Search route layout

Keep `/<type>/search` under each collection. Moving to `/search/<type>` (GitHub's layout) only pays off when search is a separate subsystem with its own engine; ours runs on the same Postgres tables as the collections, so a rename would be churn without new capability.

What would add capability is a cross-type `GET /search?q=&type=...` returning ranked mixed results. The Hub's search page currently fans out to each `/<type>/search` and `/users/search` and merges client-side. A unified endpoint would sit alongside the per-type routes, not replace them, and pairs with the mixed-type `GET /resources` idea in [`mfid_naming_transition.md`](mfid_naming_transition.md).
