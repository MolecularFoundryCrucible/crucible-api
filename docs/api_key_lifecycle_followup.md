# API-key retrieval and rotation

Status: Deprecation implemented 2026-10-12; retrieval not yet retired. Decision first recorded 2026-09-28.

## Direction

API keys are for automation (scripts, Nano, instruments); interactive users sign in through ORCID with Hub sessions. Existing keys will eventually be stored as hashes, so they can be regenerated but never read back. Every route that returns a key moves to "shown once, at creation".

## Implemented

| Route | API-key caller | Hub session |
| --- | --- | --- |
| `GET /account/apikey` | Unchanged, deprecated (`Deprecation` header, OpenAPI `deprecated`) | 403 `api_key_read_not_allowed` |
| `POST /account/apikey/regenerate` | New key returned once; previous key revoked | Same, after a recent ORCID sign-in |
| `POST /users/{user_ref}/apikey` (admin) | Unchanged, deprecated: returns the existing key or mints one | Mints only; 409 `api_key_exists` when a key exists; recent sign-in required |
| `POST /users/{user_ref}/apikey/regenerate` (admin) | New key returned once; previous key revoked | Same, after a recent ORCID sign-in |
| `POST /service_accounts`, `POST /service_accounts/{ref}/rotate_key` (admin) | Unchanged | Recent sign-in required |

A recent sign-in means the Hub session's ORCID `auth_time` is at most 600 seconds old. The API records `auth_time` from the ORCID ID token at login and keeps it on the token family, so refreshing does not renew it. Otherwise the route returns the RFC 9470 step-up challenge: HTTP 401, `WWW-Authenticate: Bearer error="insufficient_user_authentication", error_description="Recent sign-in required", max_age="600"`, and body `{"detail": {"code": "insufficient_user_authentication", "max_age": 600}}`. The Hub re-authenticates by starting `GET /auth/hub/login` with `max_age=0`, which the API forwards to ORCID together with `prompt=login`. Sessions created before migration `d94e1f2a7b05` have no `auth_time` and must sign in again before creating a key.

Hub sessions also default to normal privilege mode when `Crucible-Privilege-Mode` is omitted; API keys keep the compatibility default.

The browser `GET /auth/apikey` flow was removed in 4.0.0 (410).

## Remaining

- Retire reading existing keys after clients have moved to regeneration, then store only key hashes. That needs its own storage migration and compatibility plan.
- Regeneration revokes the previous key in the database and in the issuing worker's authentication cache; other workers may accept the old key for up to `AUTH_CACHE_TTL` (60 seconds by default).

## Authentication cache window

Accepted on 2026-10-20 as a bounded trade-off. API-key callers are resolved once and cached per worker process (`_auth_cache` in `app/utils/auth_utils.py`) for `AUTH_CACHE_TTL` seconds, 60 by default, to keep database connections off the hot path. Hub sessions are not cached; they load the principal on every request.

For up to the TTL after a change, requests with an API key can still act with the caller's previous standing. This applies to key revocation and regeneration, platform-role demotion (users and service accounts), project membership removal or demotion, instrument service-account unbinding, and access group changes. Revocation clears the entry only in the worker that handled it; nothing clears other workers or pods before expiry, and role and membership changes clear nothing.

Lowering `AUTH_CACHE_TTL` shrinks the window at the cost of more principal loads; measure connection-pool load before changing it. If immediate revocation becomes a requirement, the planned mechanism is a Postgres `LISTEN/NOTIFY` channel: every change to a key, platform role, or group membership notifies, and each worker evicts that user's cached principals.
- Decide whether `max_age` should also be verified against `auth_time` at the callback, rather than only enforcing freshness at credential creation.
