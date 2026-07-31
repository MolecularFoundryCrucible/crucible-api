# Remote (non-GCS) File Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a dataset's `AssociatedFile` entries catalog files that live outside GCS (HPC partitions, Globus-addressable paths, etc.) without Crucible verifying existence or brokering access to them.

**Architecture:** Add `storage_backend` (free text, default `"gcs"`) and `access_note` (nullable) columns to the existing `AssociatedFile` table. The literal value `"gcs"` is the only one the API treats specially — it triggers the existing signed-URL logic; every other value is uniform "catalog only" (no verification, `download_link` returns `url: null`, ingestion is rejected). No new tables, no new routes, no ACL changes.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy, PostgreSQL (prod) / SQLite in-memory (tests), pytest.

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-07-30-remote-file-records-design.md` — every requirement below traces back to it.
- Scope is limited to: `app/models/core_models.py`, `app/models/datasets.py`, `app/api/routes/files.py`, `app/api/routes/datasets.py`, one new file under `migrations/`, `tests/conftest.py`, one new test file under `tests/`, and `pyproject.toml`/`uv.lock` (Task 1 only, to add `pytest` as a dev dependency — this repo has no working test runner today). No other files change.
- `storage_backend` is free text, not an enum — never add validation constraining its values.
- `sha256_hash`/`size` stay required on `AssociatedFileCreate` for every backend — no relaxation.
- Response shapes of `GET /files/{file_id}/download_link` and `GET /datasets/{dsid}/download_links` do not change (still `{"url":..., "expires_in":...}` and `{mfid: url_or_null}` respectively) — catalog fields (`storage_backend`, `access_note`) are only ever exposed via `AssociatedFileRead`-returning routes.
- No access-control changes — remote file entries use the exact same `require_read_access`/`require_write_access` checks as GCS files today.
- This repo's `tests/` directory currently has no working pytest setup (`pytest` wasn't a project dependency, and several existing test files patch symbols that no longer exist in the codebase, e.g. `app.api.main.Session`). Fixing those pre-existing broken tests is explicitly out of scope. Every "run test" command in this plan targets only the new test file(s), never the full `tests/` directory.

---

### Task 1: Test infrastructure — pytest + shared fixtures

**Files:**
- Modify: `pyproject.toml` (add `pytest` as a dev dependency)
- Create: `tests/conftest.py`
- Create: `tests/test_associated_file_remote_backend.py`

**Interfaces:**
- Produces: `db_engine` fixture (fresh in-memory SQLite engine per test, tables created, wired into `app.api.routes.files.engine` and `app.api.routes.datasets.engine`), `client` fixture (`TestClient` over the real app), `auth_as(user_id, groups=None)` fixture function (overrides `authorize_user`; `groups` is a list of `(group_name, accessgroup_id)` tuples, defaults to `[("test_group", 1)]`).
- Consumes: `app.api.main.app`, `app.utils.auth_utils.authorize_user`, `app.models.core_models.{User, AccessGroup, UserAccessGroupLink, ResourceACL, Resource, Dataset, AssociatedFile, IngestionRequest}`.

This repo's `pyproject.toml` didn't declare `pytest` as a dependency at all (confirmed: `.venv` had no pytest installed, `pip` itself isn't even bootstrapped in the `uv`-managed venv). `SQLModel.metadata.create_all()` cannot be used with the *full* metadata on SQLite — `ScientificMetadataBinary.scientific_metadata` uses a Postgres-only `JSONB` column type that SQLite's compiler rejects. Only create the specific tables listed below.

- [ ] **Step 1: Add pytest as a dev dependency**

Run: `cd /home/roncofaber/software/crucible-api && uv add --dev pytest`

Expected: `pyproject.toml` gains a `[dependency-groups]` section with `dev = ["pytest>=<version>"]`; `uv.lock` updates.

- [ ] **Step 2: Verify pytest runs**

Run: `source .venv/bin/activate && python -m pytest --version`
Expected: prints a pytest version, no error.

- [ ] **Step 3: Write `tests/conftest.py`**

```python
import pytest
from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.api.main import app
from app.models.core_models import (
    User, AccessGroup, UserAccessGroupLink, ResourceACL, Resource, Dataset,
    AssociatedFile, IngestionRequest,
)
from app.utils.auth_utils import authorize_user
import app.api.routes.files as files_routes
import app.api.routes.datasets as datasets_routes
from fastapi.testclient import TestClient

TEST_TABLES = [
    User.__table__, AccessGroup.__table__, UserAccessGroupLink.__table__,
    ResourceACL.__table__, Resource.__table__, Dataset.__table__,
    AssociatedFile.__table__, IngestionRequest.__table__,
]


@pytest.fixture
def db_engine():
    """Fresh in-memory SQLite DB per test, wired into the route modules under test."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine, tables=TEST_TABLES)
    files_routes.engine = engine
    datasets_routes.engine = engine
    yield engine


@pytest.fixture
def client(db_engine):
    return TestClient(app)


class _FakeAccessGroup:
    def __init__(self, group_name, id):
        self.group_name = group_name
        self.id = id


@pytest.fixture
def auth_as():
    """Override authorize_user for the duration of a test.

    Usage: auth_as("0000-0000-0000-0001", groups=[("proj_x", 5)])
    """
    def _set(user_id, groups=None):
        groups = groups or [("test_group", 1)]
        fake_groups = [_FakeAccessGroup(name, gid) for name, gid in groups]
        app.dependency_overrides[authorize_user] = lambda: (user_id, fake_groups)

    yield _set
    app.dependency_overrides.pop(authorize_user, None)
```

- [ ] **Step 4: Write the first test — GCS-backend regression baseline**

Create `tests/test_associated_file_remote_backend.py`:

```python
from unittest.mock import patch
from sqlmodel import Session
from app.models.core_models import Resource, Dataset, AssociatedFile


def _make_public_dataset_with_file(db_engine, dsid, storage_path="mf-storage-prod/x/f.txt"):
    with Session(db_engine) as session:
        session.add(Resource(mfid=dsid, resource_type="dataset", public=True))
        ds = Dataset(unique_id=dsid, dataset_name="test", public=True, owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)
        af = AssociatedFile(
            filename="f.txt", storage_path=storage_path, size=10,
            sha256_hash="abc123", dataset_id=ds.id,
        )
        session.add(af)
        session.commit()
        session.refresh(af)
        return af.mfid


def test_gcs_backend_download_link_still_calls_generate_signed_url(client, db_engine, auth_as):
    file_mfid = _make_public_dataset_with_file(db_engine, "ds_pub_1")
    auth_as("0000-0000-0000-0001")

    with patch(
        "app.api.routes.files._generate_signed_url_link",
        return_value=("https://signed.example/x", 3600),
    ) as mock_sign:
        resp = client.get(f"/files/{file_mfid}/download_link")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": "https://signed.example/x", "expires_in": 3600}
    mock_sign.assert_called_once()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py -v`
Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock tests/conftest.py tests/test_associated_file_remote_backend.py
git commit -m "test: add pytest + shared fixtures for remote file record tests"
```

---

### Task 2: `AssociatedFile` table columns + migration

**Files:**
- Modify: `app/models/core_models.py:39-48` (`AssociatedFile` class)
- Create: `migrations/add_storage_backend_to_associatedfile.sql`
- Test: `tests/test_associated_file_remote_backend.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `AssociatedFile.storage_backend: str` (default `"gcs"`), `AssociatedFile.access_note: Optional[str]` — every later task relies on these two attribute names existing on the ORM class.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_associated_file_remote_backend.py`:

```python
def test_associated_file_storage_backend_defaults_to_gcs(db_engine):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_model_1", resource_type="dataset", public=True))
        ds = Dataset(unique_id="ds_model_1", dataset_name="test", owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)

        af_default = AssociatedFile(filename="a.txt", size=1, sha256_hash="h1", dataset_id=ds.id)
        af_remote = AssociatedFile(
            filename="b.txt", size=2, sha256_hash="h2", dataset_id=ds.id,
            storage_backend="globus", storage_path="globus://endpoint/path/b.txt",
            access_note="request via NERSC allocation X",
        )
        session.add_all([af_default, af_remote])
        session.commit()
        session.refresh(af_default)
        session.refresh(af_remote)

        assert af_default.storage_backend == "gcs"
        assert af_default.access_note is None
        assert af_remote.storage_backend == "globus"
        assert af_remote.access_note == "request via NERSC allocation X"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py::test_associated_file_storage_backend_defaults_to_gcs -v`
Expected: FAIL with `AttributeError: 'AssociatedFile' object has no attribute 'storage_backend'`. SQLModel silently drops unknown constructor kwargs rather than rejecting them (it doesn't raise on `storage_backend="globus"` at construction time), so the failure surfaces on the assertion, not on object creation.

- [ ] **Step 3: Add the columns to the ORM model**

In `app/models/core_models.py`, replace the `AssociatedFile` class:

```python
class AssociatedFile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    mfid: str = Field(default_factory=get_mfid_value, unique=True, index=True)
    filename: str                        # relative path within the dataset: "{safe_filename}"
    storage_path: Optional[str] = None  # GCS bucket/blob path when storage_backend="gcs"; a backend-specific location string otherwise
    storage_backend: str = Field(default="gcs")  # "gcs" triggers signed-URL generation; any other value is catalog-only
    access_note: Optional[str] = None    # how to access this file when there's no self-service path (non-gcs backends)
    size: int = Field(sa_column=Column(BigInteger()))
    sha256_hash: str
    dataset_id: int = Field(foreign_key="dataset.id")
    dataset: "Dataset" = Relationship(back_populates='associated_files')
    ingestion_requests: List["IngestionRequest"] = Relationship(back_populates="associated_file")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py::test_associated_file_storage_backend_defaults_to_gcs -v`
Expected: `1 passed`.

- [ ] **Step 5: Write the migration file**

Create `migrations/add_storage_backend_to_associatedfile.sql`:

```sql
-- Adds storage_backend and access_note to AssociatedFile for cataloging files
-- that live outside GCS (HPC partitions, Globus-addressable paths, etc).
-- storage_backend = 'gcs' triggers signed-URL generation; any other value is
-- catalog-only. Existing rows default to 'gcs' (their current, only, meaning).

ALTER TABLE associatedfile ADD COLUMN IF NOT EXISTS storage_backend TEXT NOT NULL DEFAULT 'gcs';
ALTER TABLE associatedfile ADD COLUMN IF NOT EXISTS access_note TEXT;
```

Run this manually in Google Cloud SQL Studio against the target database before deploying this branch — matches the existing convention for this repo's migrations (they are not run automatically).

- [ ] **Step 6: Run the full new test file to confirm no regressions**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py -v`
Expected: `2 passed`.

- [ ] **Step 7: Commit**

```bash
git add app/models/core_models.py migrations/add_storage_backend_to_associatedfile.sql tests/test_associated_file_remote_backend.py
git commit -m "feat: add storage_backend and access_note columns to AssociatedFile"
```

---

### Task 3: `AssociatedFileCreate`/`Read`/`Update` schema fields

**Files:**
- Modify: `app/models/datasets.py:83-109`
- Test: `tests/test_associated_file_remote_backend.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `AssociatedFileCreate.storage_backend: str = "gcs"`, `AssociatedFileCreate.access_note: Optional[str] = None`, same two fields added to `AssociatedFileRead` and `AssociatedFileUpdate` (the latter as `Optional[str] = None`, per its partial-update convention).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_associated_file_remote_backend.py`:

```python
from app.models.datasets import AssociatedFileCreate, AssociatedFileRead, AssociatedFileUpdate


def test_associated_file_create_schema_defaults_and_overrides():
    default = AssociatedFileCreate(filename="a.txt", size=1, sha256_hash="h1")
    assert default.storage_backend == "gcs"
    assert default.access_note is None

    remote = AssociatedFileCreate(
        filename="b.txt", size=2, sha256_hash="h2",
        storage_backend="lawrencium", access_note="ask PI for access",
    )
    assert remote.storage_backend == "lawrencium"
    assert remote.access_note == "ask PI for access"


def test_associated_file_read_and_update_schemas_have_new_fields():
    read = AssociatedFileRead(
        mfid="m1", dataset_id=1, dataset_mfid="ds1", filename="a.txt",
        storage_path=None, storage_backend="gcs", access_note=None,
        size=1, sha256_hash="h1",
    )
    assert read.storage_backend == "gcs"

    update = AssociatedFileUpdate()
    assert update.storage_backend is None
    assert update.access_note is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py::test_associated_file_create_schema_defaults_and_overrides tests/test_associated_file_remote_backend.py::test_associated_file_read_and_update_schemas_have_new_fields -v`
Expected: both FAIL with `AttributeError: '<ClassName>' object has no attribute 'storage_backend'`. SQLModel's default `extra="ignore"` behavior means passing `storage_backend=`/`access_note=` to these schemas today does not raise — the kwargs are silently dropped at construction, and the failure only surfaces when the test asserts on the (nonexistent) attribute afterward.

- [ ] **Step 3: Update the schemas**

In `app/models/datasets.py`, replace the three classes:

```python
class AssociatedFileCreate(SQLModel):
    '''Fields required to attach an associated file to a dataset.
    filename should be in the format {cloud-bucket}/{path-to-file}
    Ingestion is currently only supported for files in the crucible-uploads cloud-bucket.
    storage_backend defaults to "gcs"; any other value marks this as a catalog-only
    reference to a file living outside GCS (e.g. an HPC partition or Globus path) —
    Crucible does not verify existence or broker access for non-gcs files.
    '''
    filename: str
    size: int
    sha256_hash: str
    storage_backend: str = "gcs"
    access_note: Optional[str] = None


class AssociatedFileRead(SQLModel):
    '''Response model for AssociatedFile.'''
    mfid: str
    dataset_id: int
    dataset_mfid: str
    filename: str
    storage_path: Optional[str] = None
    storage_backend: str
    access_note: Optional[str] = None
    size: int
    sha256_hash: str


class AssociatedFileUpdate(SQLModel):
    '''Partial update schema for AssociatedFile. Only provided fields are modified.'''
    filename: Optional[str] = None
    storage_path: Optional[str] = None
    storage_backend: Optional[str] = None
    access_note: Optional[str] = None
    size: Optional[int] = None
    sha256_hash: Optional[str] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py -v`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/models/datasets.py tests/test_associated_file_remote_backend.py
git commit -m "feat: add storage_backend and access_note to AssociatedFile schemas"
```

---

### Task 4: End-to-end creation/update/read round trip

**Files:**
- Test: `tests/test_associated_file_remote_backend.py`

No production code changes in this task — `POST /datasets/{dsid}/files` and `PATCH /files/{file_id}` already do `**model.model_dump()` / `setattr(af, field, value)` over every field on their respective schemas, and `GET /files/{file_id}` already returns `AssociatedFileRead.model_validate(af, ...)`. Once Tasks 2 and 3 land, these routes pick up the two new fields automatically. This task's job is to prove that via real HTTP requests — a genuine acceptance check, not just unit-level schema/model tests.

**Interfaces:**
- Consumes: `client`, `db_engine`, `auth_as` fixtures from Task 1.

- [ ] **Step 1: Write the test**

Append to `tests/test_associated_file_remote_backend.py`:

```python
from app.models.core_models import AccessGroup, ResourceACL


def _grant_write_access(db_engine, dsid, user_id):
    with Session(db_engine) as session:
        session.add(Resource(mfid=dsid, resource_type="dataset", public=False))
        session.add(Dataset(unique_id=dsid, dataset_name="test", owner_orcid=user_id))
        ag = AccessGroup(group_name=dsid)
        session.add(ag)
        session.commit()
        session.refresh(ag)
        session.add(ResourceACL(mfid=dsid, accessgroup_id=ag.id, read=True, write=True))
        session.commit()
        return ag.id


def test_create_update_and_read_remote_file_round_trip(client, db_engine, auth_as):
    user_id = "0000-0000-0000-0001"
    ag_id = _grant_write_access(db_engine, "ds_roundtrip", user_id)
    auth_as(user_id, groups=[("ds_roundtrip", ag_id)])

    create_resp = client.post(
        "/datasets/ds_roundtrip/files",
        json={
            "filename": "remote.dat", "size": 42, "sha256_hash": "deadbeef",
            "storage_backend": "globus", "access_note": "ask PI for access",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    body = create_resp.json()
    assert body["storage_backend"] == "globus"
    assert body["access_note"] == "ask PI for access"
    file_mfid = body["mfid"]

    read_resp = client.get(f"/files/{file_mfid}")
    assert read_resp.status_code == 200, read_resp.text
    assert read_resp.json()["storage_backend"] == "globus"

    update_resp = client.patch(
        f"/files/{file_mfid}",
        json={"access_note": "updated: contact data steward"},
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["access_note"] == "updated: contact data steward"
    assert update_resp.json()["storage_backend"] == "globus"
```

- [ ] **Step 2: Run the test**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py::test_create_update_and_read_remote_file_round_trip -v`
Expected: `1 passed` — no production code changes should have been necessary for this to pass. If it fails, that means one of the routes has field filtering Tasks 2/3 didn't anticipate; investigate `app/api/routes/datasets.py`'s `add_associated_file` and `app/api/routes/files.py`'s `update_file`/`get_file` before making any fix.

- [ ] **Step 3: Run the full test file to confirm no regressions**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py -v`
Expected: `5 passed`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_associated_file_remote_backend.py
git commit -m "test: verify remote file create/update/read round trip via HTTP"
```

---

### Task 5: Guard download-link routes for non-GCS files

**Files:**
- Modify: `app/api/routes/files.py:94-107` (`get_file_download_link`)
- Modify: `app/api/routes/datasets.py:854-895` (`generate_dataset_download_links`)
- Test: `tests/test_associated_file_remote_backend.py`

**Interfaces:**
- Consumes: `AssociatedFile.storage_backend` (Task 2).
- Produces: no new functions — behavior change only. Response shapes unchanged (Global Constraints).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_associated_file_remote_backend.py`:

```python
def test_non_gcs_single_file_download_link_returns_null_without_calling_gcs(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_pub_2", resource_type="dataset", public=True))
        ds = Dataset(unique_id="ds_pub_2", dataset_name="test", public=True, owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)
        af = AssociatedFile(
            filename="remote.dat", size=1, sha256_hash="h1", dataset_id=ds.id,
            storage_backend="globus", storage_path="globus://endpoint/remote.dat",
        )
        session.add(af)
        session.commit()
        session.refresh(af)
        file_mfid = af.mfid

    auth_as("0000-0000-0000-0001")

    with patch("app.api.routes.files._generate_signed_url_link") as mock_sign, \
         patch("app.api.routes.files._ensure_storage_path") as mock_ensure:
        resp = client.get(f"/files/{file_mfid}/download_link")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": None, "expires_in": None}
    mock_sign.assert_not_called()
    mock_ensure.assert_not_called()


def test_non_gcs_file_in_batch_download_links_is_null(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_pub_3", resource_type="dataset", public=True))
        ds = Dataset(unique_id="ds_pub_3", dataset_name="test", public=True, owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)
        gcs_af = AssociatedFile(
            filename="local.txt", storage_path="mf-storage-prod/ds_pub_3/local.txt",
            size=1, sha256_hash="h_gcs", dataset_id=ds.id,
        )
        remote_af = AssociatedFile(
            filename="remote.dat", storage_path="globus://endpoint/remote.dat",
            storage_backend="globus", size=1, sha256_hash="h_remote", dataset_id=ds.id,
        )
        session.add_all([gcs_af, remote_af])
        session.commit()
        session.refresh(gcs_af)
        session.refresh(remote_af)
        gcs_mfid, remote_mfid = gcs_af.mfid, remote_af.mfid

    auth_as("0000-0000-0000-0001")

    with patch(
        "app.api.routes.datasets._generate_signed_url_link",
        return_value=("https://signed.example/local.txt", 3600),
    ):
        resp = client.get("/datasets/ds_pub_3/download_links")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body[gcs_mfid] == "https://signed.example/local.txt"
    assert body[remote_mfid] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py::test_non_gcs_single_file_download_link_returns_null_without_calling_gcs tests/test_associated_file_remote_backend.py::test_non_gcs_file_in_batch_download_links_is_null -v`
Expected: both FAIL, in two different ways:
- `test_non_gcs_single_file_download_link_returns_null_without_calling_gcs` errors out (not a clean assertion failure) with `ValueError: not enough values to unpack (expected 2, got 0)` — since `af.storage_backend` doesn't exist yet, the route calls the mocked `_generate_signed_url_link()` unconditionally, and unpacking its default `MagicMock()` return value into `url, expires_in` fails.
- `test_non_gcs_file_in_batch_download_links_is_null` fails as a clean `AssertionError` — `body[remote_mfid]` is `"https://signed.example/local.txt"` instead of `None`, because the mocked `_generate_signed_url_link` doesn't distinguish which file it was called for, and nothing yet skips the call for the remote file.

- [ ] **Step 3: Add the guard to `get_file_download_link`**

In `app/api/routes/files.py`, replace the `get_file_download_link` function body:

```python
@router.get("/files/{file_id}/download_link")
def get_file_download_link(auth_res: Annotated[tuple, Depends(authorize_user)], file_id: str):
    '''Returns a signed GCS download URL (1-hour expiry) for a single file.
    Returns url=None for files whose storage_backend is not "gcs" (catalog-only).'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, ag_ids)

        if af.storage_backend != "gcs":
            return {"url": None, "expires_in": None}

        storage_path = _ensure_storage_path(af, ds.unique_id, session)
        session.commit()

    url, expires_in = _generate_signed_url_link(storage_path)
    return {"url": url, "expires_in": expires_in}
```

- [ ] **Step 4: Add the guard to `generate_dataset_download_links`**

In `app/api/routes/datasets.py`, inside `generate_dataset_download_links`, replace the file-loop body:

```python
        mfid_to_path = {}
        for af in af_records:
            if af.storage_backend != "gcs":
                mfid_to_path[af.mfid] = None
                continue
            try:
                mfid_to_path[af.mfid] = _ensure_storage_path(af, dsid, session)
            except HTTPException:
                mfid_to_path[af.mfid] = None
        session.commit()
```

(The second loop, which turns `None` into `result[mfid] = None`, needs no change — it already treats a `None` path as "skip, url is null.")

- [ ] **Step 5: Run tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py -v`
Expected: `7 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/api/routes/files.py app/api/routes/datasets.py tests/test_associated_file_remote_backend.py
git commit -m "feat: skip GCS signed-URL generation for non-gcs storage_backend files"
```

---

### Task 6: Guard ingestion route for non-GCS files

**Files:**
- Modify: `app/api/routes/files.py:155-179` (`create_file_ingestion_request`)
- Test: `tests/test_associated_file_remote_backend.py`

**Interfaces:**
- Consumes: `AssociatedFile.storage_backend` (Task 2).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_associated_file_remote_backend.py`:

```python
def test_ingest_rejects_non_gcs_file(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_pub_4", resource_type="dataset", public=True))
        ds = Dataset(unique_id="ds_pub_4", dataset_name="test", public=True, owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)
        af = AssociatedFile(
            filename="remote.dat", storage_path="globus://endpoint/remote.dat",
            storage_backend="globus", size=1, sha256_hash="h1", dataset_id=ds.id,
        )
        session.add(af)
        session.commit()
        session.refresh(af)
        file_mfid = af.mfid

    auth_as("0000-0000-0000-0001")

    resp = client.post(f"/files/{file_mfid}/ingest")

    assert resp.status_code == 400, resp.text
    assert "globus" in resp.json()["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py::test_ingest_rejects_non_gcs_file -v`
Expected: ERROR (not a clean assertion failure) — without the guard, the route proceeds past the missing check and tries to open a real connection to RabbitMQ, which fails in a sandboxed/offline test environment with `pika.exceptions.AMQPConnectionError` propagating up through the test.

- [ ] **Step 3: Add the guard**

In `app/api/routes/files.py`, inside `create_file_ingestion_request`, right after `af, ds = _get_af_with_read_access(session, file_id, ag_ids)`:

```python
        af, ds = _get_af_with_read_access(session, file_id, ag_ids)

        if af.storage_backend != "gcs":
            raise HTTPException(
                status_code=400,
                detail=f"Ingestion is not supported for storage_backend='{af.storage_backend}' files.",
            )

        RMQ_ROUTING_SUFFIX = os.getenv("RMQ_ROUTING_SUFFIX", None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py::test_ingest_rejects_non_gcs_file -v`
Expected: `1 passed`.

- [ ] **Step 5: Run the full test file to confirm no regressions**

Run: `source .venv/bin/activate && python -m pytest tests/test_associated_file_remote_backend.py -v`
Expected: `8 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/api/routes/files.py tests/test_associated_file_remote_backend.py
git commit -m "feat: reject ingestion requests for non-gcs storage_backend files"
```

---

## Final check

- [ ] Run `source .venv/bin/activate && python -c "from app.api.main import app; print(len(app.openapi()['paths']))"` and confirm it prints the same route count as before this branch started (no routes added or removed — this feature only changes behavior/schemas of existing routes).
- [ ] Run the full new test file once more: `python -m pytest tests/test_associated_file_remote_backend.py -v` — expect `8 passed`.
- [ ] Re-read `docs/superpowers/specs/2026-07-30-remote-file-records-design.md` section by section and confirm every requirement has a corresponding task above (Data model ✓ Task 2/3, API/route changes ✓ Task 5/6, Access control ✓ unchanged/no task needed, Migration ✓ Task 2, Testing ✓ all tasks).
