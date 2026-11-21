---
name: add-or-change-route
description: Use when adding an API route, changing what a route accepts or returns, or deprecating or removing one - any edit under app/api/routes/ or app/models/ that changes the HTTP contract.
---

# Add or change a route

## 1. Where things go

- The route in `app/api/routes/<area>.py`; logic used by more than one route in `app/services/`.
- Request and response models in `app/models/<area>.py`. A route never takes or returns a class from `app/tables.py`; `tests/test_api_schema_has_no_tables.py` fails if one reaches the OpenAPI schema.
- Identify the caller with `Depends(authorize_user)` and check permission with the helpers in `app/utils/auth_utils.py` and `app/utils/permissions.py`.
- In an `async def` route, database and secret reads go through `run_in_threadpool`; `tests/test_async_routes_do_not_block.py` fails otherwise. Most routes are plain `def`.

## 2. Contract rules

- Identifiers in paths and bodies are MFIDs or the documented references (`docs/resource_identifiers.md`), never internal integer ids.
- Lists are paginated (`PaginatedResponse`) and follow `docs/collection_queries.md`.
- Errors a client must tell apart carry a `code`, as in `{"detail": {"code": "...", "message": "..."}}`.

## 3. Changing what exists

Released clients keep working within a major version.

- To replace a route, parameter or field: add the new one, mark the old one `deprecated=True`, and name the replacement in its description.
- To remove a deprecated route: delete it in the next major version; its path then answers 404. First confirm with the owners of the Hub, nano-crucible, crucible-ingestion and Crucible Lens that nothing calls it, since an unused route in this repository proves nothing. Delete its tests with it, and add no test that checks it is gone.

## 4. Check and record

- Test through `TestClient` with the `client`, `db_engine` and `auth_as` fixtures; cover who may and who may not.
- `uv run python -m scripts.export_openapi` writes the schema (untracked) to inspect it.
- Add one line under `Unreleased` in `CHANGELOG.md`, under Breaking changes when a client must change.
- Name the clients affected in the pull request; changes to their repositories are made there.
