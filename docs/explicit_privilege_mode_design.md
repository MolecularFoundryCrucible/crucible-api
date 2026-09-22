# Explicit privilege mode

## Status

Implemented on the feature branch as an additive API contract. No database migration is required.

The feature separates a platform administrator's assigned role from the authority applied to an individual request. Existing clients retain their current behavior when they omit the new header.

## Request contract

Authenticated routes accept the optional header:

```http
Crucible-Privilege-Mode: normal
```

or:

```http
Crucible-Privilege-Mode: elevated
```

The API resolves it as follows:

| Request | Human platform administrator | Other caller |
|---|---|---|
| Header omitted | Existing elevated behavior | Existing behavior |
| `normal` | Ordinary ACL, group, public, and resource permissions only | Existing non-admin behavior |
| `elevated` | Platform-administrator authority | `403 Forbidden` |

An unsupported value returns `422 Unprocessable Content`. Service accounts cannot explicitly request elevated mode. An existing administrator service account retains its previous authority only when the header is omitted, as a temporary compatibility behavior.

The Hub should send `normal` for routine requests and `elevated` only for a deliberate platform-administration workflow. The API never retries or falls back to elevated mode.

## Response contract

After authentication and mode resolution, responses return the applied mode:

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

Existing stricter cache directives remain intact. The request and successful response headers are published in the checked-in OpenAPI contract.

## Account profile

`GET /account/profile` reports assigned eligibility separately from capabilities effective for the current request:

```json
{
  "authorization": {
    "platform_role": "admin",
    "effective_privilege_mode": "normal",
    "can_elevate": true
  },
  "capabilities": {
    "can_manage_service_accounts": false,
    "can_create_project": true,
    "can_register_instrument": true,
    "can_create_sample": true,
    "can_create_dataset": true,
    "can_create_for_others": false
  }
}
```

`platform_role` is the assigned platform entitlement. `can_elevate` is true only for a human platform administrator. `effective_privilege_mode` and `capabilities` describe the mode applied to that profile request. Resource-specific capabilities continue to describe the effective mode used for their request.

The Hub can discover elevation eligibility with one normal profile request. It should refetch the profile in elevated mode after activation to obtain elevated capabilities.

## Implementation

Authentication loads the original principal, then resolves an authorization context once per request. Normal mode derives an effective principal without the administrator platform role while preserving its identity and ACL group memberships. Existing route dependencies and permission helpers receive that effective principal, including routes that inspect `is_platform_admin` directly.

The original principal remains available for eligibility reporting and audit data. This keeps privilege selection centralized instead of adding mode checks to individual routes.

## Operational audit logs

Explicit and compatibility-default elevated requests emit a structured `privileged_request` application log containing:

- Actor identifier
- Applied mode and whether it was explicit or inherited from the compatibility default
- HTTP method and route template
- Target MFID when present in the route
- Request ID
- Response status
- Duration
- UTC timestamp

Rejected explicit elevation attempts are logged after authentication. Credentials, request bodies, signed URLs, and API keys are not logged. Phase one uses application logs and does not add an audit table or Alembic revision.

## Client behavior

- Send `normal` centrally for routine collection, detail, graph, search, and mutation requests.
- Use `authorization.can_elevate` to expose deliberate administration entry points.
- Send `elevated` only from a trusted server-side administration client.
- Verify that the response `Crucible-Privilege-Mode` matches the requested mode.
- Never retry a `403` or `404` with elevated mode automatically.
- Include privilege mode in cache identity or isolate elevated request caches.
- End elevation on explicit deactivation, expiration, logout, account or server change, and browser-session termination.

The current header selects authority but is not a second authentication factor. A stolen administrator credential can still request elevation.

## Future work

- Change omitted mode to normal only in a future breaking API version after clients have migrated.
- Consider short-lived, server-issued privilege leases with recent authentication, expiration, revocation, and optional justification.
- Consider durable database-backed audit history if application-log retention and querying are insufficient.
- Define explicit privilege propagation for future asynchronous operations that outlive the initiating request.
