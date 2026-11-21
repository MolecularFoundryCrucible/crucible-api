# Explicit privilege mode

The `Crucible-Privilege-Mode` request header selects whether a platform administrator's privileges apply to a request. This page covers how the mode is resolved, the response headers, the account profile and the audit log; roles and grants are described in [Permissions](permissions.md).

## Modes

| Mode | Effect |
|---|---|
| `normal` | The caller is authorized only through their own groups and grants. Platform role is ignored. |
| `elevated` | Platform role applies, and the caller bypasses permission checks accordingly. |

Routes that require authentication accept the optional header with either value. Any other value returns 422.

## Mode resolution

| Request | Human platform administrator | Service account with platform role `admin` | Other caller |
|---|---|---|---|
| Header omitted, API key | `elevated` (compatibility default) | `elevated` (compatibility default) | `normal` |
| Header omitted, session | `normal` | Not applicable | `normal` |
| `normal` | `normal` | `normal` | `normal` |
| `elevated` | `elevated` | 403 | 403 |

- The compatibility default applies to API keys only: an administrator's API key, human or service account, acts elevated when the header is omitted. This will be changed in a later revision.
- The API never retries a request or falls back to elevated mode.
- Making a service account a platform administrator (`PATCH /service_accounts/{service_account_ref}` with `platform_role: admin`) requires a human administrator in elevated mode in a browser session authenticated within the last ten minutes. An API key receives 403 `hub_session_required`; see [Authentication](sign_in.md#operations-that-require-recent-browser-authentication).

## Response headers

Responses to requests whose mode was resolved carry the applied mode:

```http
Crucible-Privilege-Mode: normal
Vary: Authorization, Crucible-Privilege-Mode
Cache-Control: private
X-Request-ID: 7ff4f79f-86d3-49d4-b2e8-614326cab836
```

Elevated responses use:

```http
Cache-Control: private, no-store
```

In elevated mode this replaces any `Cache-Control` set by the route. In normal mode a route's own `Cache-Control` is kept, and `private` is set only when the route set none. Requests rejected before the mode is resolved (401, or 403 for an ineligible `elevated`) receive no `Crucible-Privilege-Mode` header. The request and response headers are published in the OpenAPI document.

## Account profile

`GET /account/profile` reports the caller's eligibility separately from the capabilities in effect for the current request (excerpt):

```json
{
  "authorization": {
    "platform_role": "admin",
    "effective_privilege_mode": "normal",
    "can_elevate": true
  },
  "capabilities": {
    "can_manage_service_accounts": false,
    "can_manage_users": false,
    "can_create_project": true,
    "can_register_instrument": true,
    "can_create_sample": true,
    "can_create_dataset": true,
    "can_create_for_others": false
  }
}
```

`platform_role` is the assigned platform role. `effective_privilege_mode` and `capabilities` describe the mode applied to the profile request, and resource-specific `capabilities` in other responses describe the mode of their own request. A client discovers eligibility with one profile request in normal mode, and requests the profile again in elevated mode to obtain the elevated capabilities.

## Implementation

`authorize_user` (`app/utils/auth_utils.py`) resolves the mode once per request with `resolve_authorization_context` (`app/utils/permissions.py`). In normal mode routes receive a copy of the caller's `Principal` with platform role `none` in place of `admin`, with identity and group memberships unchanged, so every permission check, including direct tests of `is_platform_admin`, sees an ordinary caller. The original principal is retained for the account profile and the audit log.

## Audit log

Every elevated request, explicit or by the compatibility default, and every rejected request for `elevated` from an authenticated caller, writes one application log line at INFO level: a JSON object with `"event": "privileged_request"` and these fields.

| Field | Content |
|---|---|
| `actor_id` | The caller's `unique_id` |
| `privilege_mode` | Always `elevated` |
| `mode_source` | `explicit` or `compatibility_default` |
| `method`, `route` | HTTP method and route template |
| `target_mfid` | The `mfid` or `*_mfid` path parameter, or null |
| `request_id` | The `X-Request-ID` value |
| `status` | Response status code |
| `duration_ms` | Request duration |
| `timestamp` | UTC time, ISO 8601 |

Credentials, API keys, request bodies and signed URLs are not logged. The log is the only record; no database table stores privileged requests.

## Client guidance

- Send `normal` on routine collection, detail, graph, search and mutation requests.
- Use `authorization.can_elevate` to expose administration entry points, and send `elevated` only for deliberate administration from a trusted server-side client.
- Verify that the response `Crucible-Privilege-Mode` matches the requested mode.
- Never retry a 403 or 404 in elevated mode automatically.
- Include the privilege mode in cache keys, or keep elevated responses out of shared caches.
- End elevation on explicit deactivation, expiry, sign-out, account or server change, and end of the browser session.

The header selects authority but is not a second authentication factor: a stolen administrator credential can request elevation.
