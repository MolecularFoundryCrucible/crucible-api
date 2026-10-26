# Database engine: housekeeping follow-up

Status: proposed 2026-10-26, not scheduled. No behavior change intended.

## Today

`get_engine()` in `app/api/dependencies.py` creates one engine on first use and caches it. Route and auth code call `get_engine()` where they need it (`Session(get_engine())`); nothing creates an engine while the code loads. Tests point it at a test database with the `use_engine` fixture in `tests/conftest.py`, which replaces the engine factory and clears the cache.

The Hub sign-in routes (`app/api/routes/hub_auth.py`) already receive the engine as a FastAPI dependency (`engine=Depends(get_engine)`), and their tests use `app.dependency_overrides`.

## Proposal

Move the remaining routes to the same dependency style, which is the pattern in the FastAPI documentation ("SQL (Relational) Databases", `SessionDep = Annotated[Session, Depends(get_session)]`):

1. Add `get_session()` that yields `Session(get_engine())`, and a `SessionDep` alias, next to `get_engine()`.
2. Change routes to take `session: SessionDep` instead of opening `Session(get_engine())` themselves, one router per PR. Helpers that take an engine keep taking it.
3. Replace the `use_engine` fixture with `app.dependency_overrides[get_engine]` once no code calls `get_engine()` directly outside dependencies.

This touches every route signature, so it is a separate change from the self-hosting work. The OpenAPI document must not change; check with the snapshot in `openapi/`.
