# Error responses: housekeeping follow-up

Status: proposed 2026-10-26, not scheduled. No behavior change intended.

## Today

`app/api/errors.py` (added with the self-hosting feature switches) is the one place that turns application errors into HTTP answers: it registers the handlers for `FeatureDisabled` and `StorageNotConfigured` (both `501`) and holds their documented OpenAPI responses. The exceptions themselves stay where they are raised (`app/config.py`, `app/services/storage_downloads.py`), so service code does not depend on HTTP.

Everything else is still documented inline on each route, and several descriptions are copied verbatim:

- the `401` "Hub sessions need a recent ORCID sign-in (`insufficient_user_authentication`)" response on four routes;
- the `409` `storage_location_invalid` response on three routes (`files.py` twice, `datasets.py` once);
- the `409` `already_in_use` response for usernames and emails on two routes, and the `409` `filename_exists` response on two;
- three pairs of `307` sign-in redirect descriptions in the authentication routes.

Structured error bodies (`{"detail": {"code": ..., ...}}`) are built by hand at about 17 call sites in routes, utilities, and services.

## Proposal

1. Move each repeated response description into `app/api/errors.py` as a named constant (or a small function when the text depends on an argument), and reference it from the routes. The OpenAPI document must not change; check with the snapshot in `openapi/`.
2. Consider one helper for structured error bodies, for example `error_detail(code, message, **fields)`, used by the existing call sites, so every coded error has the same shape.
3. Leave the Hub sign-in validation handler in `app/api/main.py`; it depends on the Hub authentication headers defined there.

## Later

The formal standard for error bodies is RFC 9457 Problem Details (`application/problem+json` with `type`, `title`, `status`, `detail`). Adopting it would change every error response and every client, so it is a separate, versioned decision, not part of this housekeeping.
