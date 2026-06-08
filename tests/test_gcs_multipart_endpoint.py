import sys
import pytest
import os
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    mock_config = {
        'hostname': 'test.example.com',
        'api_path': 'api',
        'orcid_client_id': 'test_client_id',
    }

    # Clear any partially imported app modules so patches take effect cleanly.
    for key in list(sys.modules):
        if key.startswith('app.'):
            del sys.modules[key]

    with patch('app.api.dependencies.get_api_config', return_value=mock_config), \
         patch('app.api.dependencies.get_engine', return_value=MagicMock()), \
         patch('app.general_utils.get_secret', return_value='test_secret'):
        from app.api.routes.gcs_storage import router

    test_app = FastAPI()
    test_app.include_router(router, prefix="/api")
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app)


def _make_mock_dataset(dsid="ds-001"):
    ds = MagicMock()
    ds.id = 1
    ds.unique_id = dsid
    return ds


class TestInitiateMultipartUpload:

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_returns_token_and_upload_id(
        self, mock_mint, mock_session_cls, mock_write_access, mock_auth, client
    ):
        """Happy path: returns access token, upload_id, and GCS coordinates."""
        mock_auth.return_value = ('user-orcid-001', [MagicMock(id=1)])
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

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_dedup_returns_existing_file(
        self, mock_mint, mock_session_cls, mock_write_access, mock_auth, client
    ):
        """When SHA256 matches an existing file, returns existing_file without minting a token."""
        mock_auth.return_value = ('user-orcid-001', [MagicMock(id=1)])

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

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.routes.gcs_storage.Session')
    def test_dataset_not_found_returns_404(self, mock_session_cls, mock_auth, client):
        """Returns 404 when the dataset does not exist."""
        mock_auth.return_value = ('user-orcid-001', [MagicMock(id=1)])

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session
        session.exec.return_value.one_or_none.return_value = None

        body = {"filename": "data.h5", "size": 1024}
        resp = client.post("/api/datasets/nonexistent/upload/initiate/multipart", json=body)

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.routes.gcs_storage.Session')
    def test_zero_size_returns_400(self, mock_session_cls, mock_auth, client):
        """Returns 400 when size is 0."""
        mock_auth.return_value = ('user-orcid-001', [MagicMock(id=1)])

        body = {"filename": "data.h5", "size": 0}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)

        assert resp.status_code == 400
        assert "size" in resp.json()["detail"].lower()

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_gcs_failure_returns_503(
        self, mock_mint, mock_session_cls, mock_write_access, mock_auth, client
    ):
        """Returns 503 when token minting fails."""
        mock_auth.return_value = ('user-orcid-001', [MagicMock(id=1)])
        mock_mint.side_effect = Exception("GCS unavailable")

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session

        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset(), None]
        session.exec.return_value = exec_result

        body = {"filename": "data.h5", "size": 1024, "sha256_hash": "abc123"}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)

        assert resp.status_code == 503

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.routes.gcs_storage.require_write_access')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_path_traversal_rejected(
        self, mock_mint, mock_session_cls, mock_write_access, mock_auth, client
    ):
        """Returns 400 for filenames that strip to empty after removing traversal components."""
        mock_auth.return_value = ('user-orcid-001', [MagicMock(id=1)])

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session
        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset(), None]
        session.exec.return_value = exec_result

        body = {"filename": "/", "size": 1024}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)
        assert resp.status_code == 400
        assert "filename" in resp.json()["detail"].lower()
