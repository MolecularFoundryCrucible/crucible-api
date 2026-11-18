---
name: add-or-change-route
description: Add an API route, change what one accepts or returns, deprecate one or remove one. Use for any edit under app/api/routes/ or app/models/ that changes the HTTP contract.
---

# Add or change a route

## 1. Where things go

- The route in `app/api/routes/<area>.py`; logic used by more than one route in `app/services/`.
- Request and response models in `app/models/<area>.py`. A route never takes or returns a class from `app/tables.py`; a test fails if one appears in the OpenAPI schema.
- Identify the caller with `Depends(authorize_user)` and check permission with the helpers in `app/utils/auth_utils.py` and `app/utils/permissions.py`.
- In an `async def` route, database and secret reads go through `run_in_threadpool`; a test fails otherwise. Most routes are plain `def`.

## 2. Contract rules

- Identifiers in paths and bodies are MFIDs or the documented references (`docs/resource_identifiers.md`), never internal integer ids.
- Lists are paginated (`PaginatedResponse`) and follow `docs/collection_queries.md`.
- Errors a client must tell apart carry a `code`, as in `{"detail": {"code": "...", "message": "..."}}`.

## 3. Changing what exists

Released clients keep working within a major version.

- To replace a route, parameter or field: add the new one, mark the old one `deprecated=True` and name the replacement in its description.
- To remove a route: keep the path and answer 410 with `removed("<what to use instead>")` from `app/api/removed.py`, and add it to `tests/test_removed_routes.py`. Do this only when the Hub, nano-crucible, the ingestion pipeline and Crucible Lens no longer call it; ask their owners, an unused route in this repository proves nothing.

## 4. Check and record

- Test through `TestClient` with the `client`, `db_engine` and `auth_as` fixtures; cover who may and who may not.
- `uv run python -m scripts.export_openapi` writes the schema (untracked) if you need to inspect it.
- Add one line under `Unreleased` in `CHANGELOG.md`, marked **Breaking** when a client must change.
- Tell the user which clients are affected; do not edit other repositories.
