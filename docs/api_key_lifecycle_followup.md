# API-key retrieval and rotation

Status: Deferred. Decision recorded 2026-09-28.

## Current behavior and risk

`GET /account/apikey` accepts a valid Hub access token and returns the caller's existing API key when one exists. Someone holding a stolen Hub token could retrieve a longer-lived key that remains usable after Hub logout or token-family revocation. Hiding API-key controls in the Hub does not prevent direct calls to this route.

The user chose to document this risk without changing current authorization or client behavior. No endpoint is removed, restricted, or newly deprecated by this decision.

## Intended direction

Retire retrieval of existing API keys and retain rotation as the way to obtain a replacement key. Plan to reveal the newly generated key only in the rotation response, not through subsequent GET requests. Rotation must replace the prior credential; define and test revocation across workers and any authentication caches.

Before implementing, audit consumers of `GET /account/apikey`, the legacy browser `GET /auth/apikey` flow, and their aliases. Define the replacement onboarding flow and a backward-compatible deprecation and migration schedule. Decide fresh-authentication requirements for rotation: rotation alone does not prevent a stolen Hub token from obtaining a new long-lived credential if the ordinary session can invoke it.

Consider storing only hashes of API keys once retrieval is retired. That requires a separate storage, migration, and compatibility plan; it is not part of this decision's implementation.

Coordinate API documentation, OpenAPI, Python-client guidance, and a separately authorized Hub update when this work is scheduled. No database, credential, route, or deployment changes were made when recording this follow-up.
