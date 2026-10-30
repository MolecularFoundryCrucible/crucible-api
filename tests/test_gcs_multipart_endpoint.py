import sys
import pytest
import os
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    # Import normally rather than purging sys.modules to re-import under patches.
    # The purge re-registered every SQLModel table against a MetaData that survives
    # it, so this file could not coexist with any other test that imports the app first.
    from app.api.routes import gcs_storage

    test_app = FastAPI()
    test_app.include_router(gcs_storage.router, prefix="/api")
    return test_app


@pytest.fixture(autouse=True)
def mock_engine(use_engine):
    use_engine(MagicMock())


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
    """Dependency override: skip real auth, return a test principal."""
    from app.utils.permissions import GroupMembership, Principal, Role
    return Principal(
        user_id='user-orcid-001',
        groups=(GroupMembership(id=1, group_name='test_group', role=Role.OWNER),),
    )


class TestInitiateMultipartUpload:

    @patch('app.api.routes.gcs_storage.require_permission')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_returns_token_and_upload_id(
        self, mock_mint, mock_session_cls, mock_require_permission, client, app
    ):
        """Happy path: returns access token, upload_id, and GCS coordinates."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth
        mock_mint.return_value = ("ya29.fake_token", "2026-06-08T16:00:00+00:00")

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session

        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset()]
        exec_result.first.return_value = None
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
        from app.utils.permissions import Permission
        assert mock_require_permission.call_args.args[3] is Permission.CREATE

    @patch('app.api.routes.gcs_storage.require_permission')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_dedup_returns_existing_file(
        self, mock_mint, mock_session_cls, mock_require_permission, client, app
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
        existing_af.storage_generation = None
        existing_af.dataset.unique_id = "ds-001"

        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [ds]
        exec_result.first.side_effect = [None, existing_af]
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

    @patch('app.api.routes.gcs_storage.require_permission')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_gcs_failure_returns_503(
        self, mock_mint, mock_session_cls, mock_require_permission, client, app
    ):
        """Returns 503 when token minting fails."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth
        mock_mint.side_effect = Exception("GCS unavailable")

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session

        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset()]
        exec_result.first.return_value = None
        session.exec.return_value = exec_result

        body = {"filename": "data.h5", "size": 1024, "sha256_hash": "abc123"}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)

        assert resp.status_code == 503

    @patch('app.api.routes.gcs_storage.require_permission')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_path_traversal_rejected(
        self, mock_mint, mock_session_cls, mock_require_permission, client, app
    ):
        """Returns 400 for filenames that strip to empty after removing traversal components."""
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth

        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session
        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset()]
        exec_result.first.return_value = None
        session.exec.return_value = exec_result

        body = {"filename": "/", "size": 1024}
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json=body)
        assert resp.status_code == 400
        assert "filename" in resp.json()["detail"].lower()

    @patch('app.api.routes.gcs_storage.require_permission')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_reserved_files_segment_is_rejected(self, mock_mint, mock_session_cls, mock_require_permission, client, app):
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth
        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session
        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset()]
        exec_result.first.return_value = None
        session.exec.return_value = exec_result
        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json={"filename": "_files/x.bin", "size": 10})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Filenames starting with _files/ are reserved"
        mock_mint.assert_not_called()


class TestMintUploadToken:

    def _mint(self, monkeypatch, dsid="ds-001", flag=None):
        from app.api.routes import gcs_storage
        if flag is None:
            monkeypatch.delenv("LEGACY_UPLOAD_TOKEN_DOWNSCOPE", raising=False)
        else:
            monkeypatch.setenv("LEGACY_UPLOAD_TOKEN_DOWNSCOPE", flag)
        source = MagicMock()
        scoped = MagicMock()
        source.with_scopes.return_value = scoped
        scoped.token, scoped.expiry = "broad", MagicMock(isoformat=lambda: "broad-expiry")
        with patch.object(gcs_storage, "key_credentials", return_value=source), \
             patch("google.auth.downscoped.Credentials") as downscoped_cls:
            downscoped_cls.return_value.token = "narrow"
            downscoped_cls.return_value.expiry = MagicMock(isoformat=lambda: "narrow-expiry")
            result = gcs_storage._mint_upload_token(dsid)
        return result, downscoped_cls, scoped

    def test_token_is_limited_to_the_dataset_prefix(self, monkeypatch):
        result, downscoped_cls, scoped = self._mint(monkeypatch, dsid="0abc")
        assert result == ("narrow", "narrow-expiry")
        kwargs = downscoped_cls.call_args.kwargs
        assert kwargs["source_credentials"] is scoped
        (rule,) = kwargs["credential_access_boundary"].rules
        assert rule.available_resource == "//storage.googleapis.com/projects/_/buckets/mf-storage-prod"
        assert rule.available_permissions == ("inRole:roles/storage.objectAdmin",)
        assert rule.availability_condition.expression == (
            "resource.name.startsWith('projects/_/buckets/mf-storage-prod/objects/0abc/')"
            " && !resource.name.startsWith('projects/_/buckets/mf-storage-prod/objects/0abc/_files/')"
        )
        downscoped_cls.return_value.refresh.assert_called_once()

    def test_kill_switch_returns_the_service_account_token(self, monkeypatch):
        result, downscoped_cls, scoped = self._mint(monkeypatch, flag="false")
        assert result == ("broad", "broad-expiry")
        downscoped_cls.assert_not_called()

    @patch('app.api.routes.gcs_storage.require_permission')
    @patch('app.api.routes.gcs_storage.Session')
    def test_exchange_failure_returns_503(self, mock_session_cls, mock_require_permission, client, app, monkeypatch):
        from app.api.routes import gcs_storage
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth
        monkeypatch.delenv("LEGACY_UPLOAD_TOKEN_DOWNSCOPE", raising=False)
        session = MagicMock()
        session.exec.return_value.one_or_none.return_value = _make_mock_dataset()
        session.exec.return_value.first.return_value = None
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(gcs_storage, "key_credentials", return_value=MagicMock()), \
             patch("google.auth.downscoped.Credentials") as downscoped_cls, \
             patch.object(gcs_storage.multipart, "filename_reserved", return_value=False):
            downscoped_cls.return_value.refresh.side_effect = RuntimeError("sts unreachable")
            resp = client.post("/api/datasets/ds-001/upload/initiate/multipart",
                               json={"filename": "a.bin", "size": 10})
        assert resp.status_code == 503
        assert "broad" not in resp.text


class TestLegacyUploadDeprecation:

    def test_openapi_marks_the_legacy_upload_routes_deprecated(self, app):
        paths = app.openapi()["paths"]
        for route in ("initiate", "initiate/multipart", "complete"):
            assert paths[f"/api/datasets/{{dataset_mfid}}/upload/{route}"]["post"]["deprecated"] is True

    @patch('app.api.routes.gcs_storage.require_permission')
    @patch('app.api.routes.gcs_storage.Session')
    @patch('app.api.routes.gcs_storage._mint_upload_token')
    def test_legacy_initiate_sends_the_deprecation_header(self, mock_mint, mock_session_cls, mock_require_permission, client, app):
        from app.api.routes.gcs_storage import LEGACY_UPLOAD_DEPRECATION
        from app.utils.auth_utils import authorize_user
        app.dependency_overrides[authorize_user] = _fake_auth
        mock_mint.return_value = ("ya29.fake_token", "2026-06-08T16:00:00+00:00")
        session = MagicMock()
        mock_session_cls.return_value.__enter__.return_value = session
        exec_result = MagicMock()
        exec_result.one_or_none.side_effect = [_make_mock_dataset()]
        exec_result.first.return_value = None
        session.exec.return_value = exec_result

        resp = client.post("/api/datasets/ds-001/upload/initiate/multipart", json={"filename": "data.h5", "size": 1024, "sha256_hash": "abc123"})

        assert resp.status_code == 200
        assert resp.headers["Deprecation"] == LEGACY_UPLOAD_DEPRECATION == "@1793318400"
