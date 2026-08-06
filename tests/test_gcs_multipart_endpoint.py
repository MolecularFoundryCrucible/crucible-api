import sys
import pytest
import os
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    # Import normally and neutralise the module-level engine afterwards, rather
    # than purging sys.modules to re-import under patches. The purge re-registered
    # every SQLModel table against a MetaData that survives it, so this file could
    # not coexist with any other test that imports the app first.
    from app.api.routes import gcs_storage

    gcs_storage.engine = MagicMock()

    test_app = FastAPI()
    test_app.include_router(gcs_storage.router, prefix="/api")
    return test_app


@pytest.fixture
def client(app):
    app.dependency_overrides = {}  # reset between tests
    return TestClient(app)


def _make_mock_dataset(dsid="ds-001"):
    ds = MagicMock()
    ds.id = 1
    ds.unique_id = dsid
    return ds


def _fake_auth():
    """Dependency override: skip real auth, return a test user."""
    return ('user-orcid-001', [MagicMock(id=1)])


class TestInitiateMultipartUpload:

    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_returns_token_and_upload_id(
        self, mock_mint, mock_session_cls, mock_write_access, client, app
    ):
        """Happy path: returns access token, upload_id, and GCS coordinates."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth
        mock_mint.return_value = ("ya29.fake_token", "2026-06-08T16:00:00+00:00")

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session

        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset(), None]
        session.exec.return_value = exec_result

        body = {"filename": "data.h5", "size": 1024 * 1024 * 64, "sha256_hash": "abc123"}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)

        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"] == "ya29.fake_token"
        assert data["token_expiry"] == "2026-06-08T16:00:00+00:00"
        assert data["bucket"] == "mf-storage-prod"
        assert data["object_prefix"] == "ds-001/"
        assert data["upload_id"] is not None
        assert data["existing_file"] is None

    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_dedup_returns_existing_file(
        self, mock_mint, mock_session_cls, mock_write_access, client, app
    ):
        """When SHA256 matches an existing file, returns existing_file without minting a token."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session

        ds = _make_mock_dataset()
        existing_af = MagicMock()
        existing_af.mfid = "mf-file-001"
        existing_af.dataset_id = 1
        existing_af.filename = "data.h5"
        existing_af.storage_path = "mf-storage-prod/ds-001/data.h5"
        existing_af.size = 1024
        existing_af.sha256_hash = "abc123"
        existing_af.storage_backend = "gcs"
        existing_af.access_note = None
        existing_af.dataset.unique_id = "ds-001"

        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [ds, existing_af]
        session.exec.return_value = exec_result

        body = {"filename": "data.h5", "size": 1024, "sha256_hash": "abc123"}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)

        assert resp.status_code == 200
        data = resp.json()
        assert data["existing_file"] is not None
        assert data["existing_file"]["mfid"] == "mf-file-001"
        assert data["access_token"] is None
        mock_mint.assert_not_called()

    @patch('app.api.routes.gcs_storage.Session')
    def test_dataset_not_found_returns_404(self, mock_session_cls, client, app):
        """Returns 404 when the dataset does not exist."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session
        session.exec.return_value.one_or_none.return_value = None

        body = {"filename": "data.h5", "size": 1024}
        resp = client.post("/api/datasets/nonexistent/upload/initiate/multipart", json=body)

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_zero_size_returns_400(self, client, app):
        """Returns 400 when size is 0 — checked before auth."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth

        body = {"filename": "data.h5", "size": 0}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)

        assert resp.status_code == 400
        assert "size" in resp.json()["detail"].lower()

    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_gcs_failure_returns_503(
        self, mock_mint, mock_session_cls, mock_write_access, client, app
    ):
        """Returns 503 when token minting fails."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth
        mock_mint.side_effect = Exception("GCS unavailable")

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session

        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset(), None]
        session.exec.return_value = exec_result

        body = {"filename": "data.h5", "size": 1024, "sha256_hash": "abc123"}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)

        assert resp.status_code == 503

    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_path_traversal_rejected(
        self, mock_mint, mock_session_cls, mock_write_access, client, app
    ):
        """Returns 400 for filenames that strip to empty after removing traversal components."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session
        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset(), None]
        session.exec.return_value = exec_result

        body = {"filename": "/", "size": 1024}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)
        assert resp.status_code == 400
        assert "filename" in resp.json()["detail"].lower()
