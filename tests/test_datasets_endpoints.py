import pytest
import json
import os
from unittest.mock import patch, MagicMock, mock_open
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    """Create the FastAPI app with mocked dependencies."""
    with patch.dict(os.environ, {
        'DB_CONF_FILE': 'test_db_config.yaml',
        'API_CONF_FILE': 'test_api_config.yaml',
        'HOME': '/tmp'
    }):
        mock_config_content = """
hostname: test.example.com
api_path: api
orcid_client_id: test_client_id
"""
        with patch('builtins.open', mock_open(read_data=mock_config_content)), \
             patch('app.utils.sql_utils.setup_engine') as mock_engine, \
             patch('app.general_utils.get_secret', return_value='test_secret'), \
             patch('yaml.safe_load', return_value={
                 'hostname': 'test.example.com',
                 'api_path': 'api',
                 'orcid_client_id': 'test_client_id'
             }):
            from app.api.main import app
            return app


@pytest.fixture
def client(app):
    """Create a test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def mock_auth():
    """Mock authorization that returns test access group info."""
    return ('test_user', [1, 2, 3])


@pytest.fixture
def sample_dataset():
    """Sample dataset data for testing."""
    return {
        "unique_id": "test_dataset_001",
        "dataset_name": "Test Dataset",
        "description": "A test dataset for unit testing",
        "public": False,
        "owner_orcid": "0000-0000-0000-0001",
        "project_id": "test_project"
    }


class TestCreateDataset:
    """Test cases for POST /datasets endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    @patch('app.api.main.sql_create_ds_acl')
    def test_create_dataset_success(self, mock_acl, mock_add_update, mock_session, mock_auth_user, client, sample_dataset):
        """Test successful dataset creation."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance

        # Mock dataset creation
        mock_dataset = MagicMock()
        mock_dataset.model_dump.return_value = sample_dataset
        mock_add_update.return_value = mock_dataset

        # Mock access groups
        mock_session_instance.exec.return_value.one_or_none.return_value = MagicMock()

        response = client.post("/datasets",
                             json=sample_dataset,
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200
        assert response.json()["unique_id"] == sample_dataset["unique_id"]

    @patch('app.utils.auth_utils.authorize_user')
    def test_create_dataset_unauthorized(self, mock_auth_user, client, sample_dataset):
        """Test dataset creation without authorization."""
        mock_auth_user.side_effect = Exception("Unauthorized")

        response = client.post("/datasets", json=sample_dataset)

        assert response.status_code in [401, 403, 422]

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    def test_create_dataset_invalid_data(self, mock_session, mock_auth_user, client):
        """Test dataset creation with invalid data."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        invalid_dataset = {"invalid_field": "invalid_value"}

        response = client.post("/datasets",
                             json=invalid_dataset,
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 422


class TestUpdateDataset:
    """Test cases for PATCH /datasets/{dsid} endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    def test_update_dataset_success(self, mock_add_update, mock_session, mock_auth_user, client):
        """Test successful dataset update."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session and dataset query
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.all.return_value = [MagicMock()]
        mock_session_instance.exec.return_value.one_or_none.return_value = MagicMock()

        # Mock updated dataset
        mock_dataset = MagicMock()
        mock_add_update.return_value = mock_dataset

        update_data = {"description": "Updated description"}
        response = client.patch("/datasets/test_dataset_001",
                              json=update_data,
                              headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    def test_update_dataset_not_found(self, mock_session, mock_auth_user, client):
        """Test updating non-existent dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock empty result
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.all.return_value = []

        update_data = {"description": "Updated description"}
        response = client.patch("/datasets/nonexistent_dataset",
                              json=update_data,
                              headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 404


class TestDatasetUpload:
    """Test cases for POST /datasets/{dsid}/upload endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.api.main.Path')
    @patch('builtins.open')
    def test_upload_file_success(self, mock_open_file, mock_path, mock_session, mock_auth_user, client):
        """Test successful file upload to dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session and dataset
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_dataset = MagicMock()
        mock_dataset.unique_id = "test_dataset_001"
        mock_session_instance.exec.return_value.one_or_none.return_value = mock_dataset

        # Mock file operations
        mock_path.return_value.mkdir = MagicMock()
        mock_file = MagicMock()
        mock_file.filename = "test_file.txt"
        mock_file.read.return_value = b"test content"

        # Create form data with file
        files = {"files": ("test_file.txt", b"test content", "text/plain")}
        response = client.post("/datasets/test_dataset_001/upload",
                             files=files,
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200


class TestDatasetIngest:
    """Test cases for POST /datasets/{dsid}/ingest endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.rmq_utils.send_pika_message')
    @patch('app.general_utils.get_secret')
    def test_request_ingestion_success(self, mock_secret, mock_send_msg, mock_session, mock_auth_user, client):
        """Test successful ingestion request."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])
        mock_secret.return_value = 'test_password'

        # Mock session and dataset
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_dataset = MagicMock()
        mock_dataset.file_to_upload = "test_file.txt"
        mock_session_instance.exec.return_value.one_or_none.return_value = mock_dataset

        response = client.post("/datasets/test_dataset_001/ingest",
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200
        mock_send_msg.assert_called_once()

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    def test_request_ingestion_no_file(self, mock_session, mock_auth_user, client):
        """Test ingestion request without file."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock dataset without file
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_dataset = MagicMock()
        mock_dataset.file_to_upload = None
        mock_session_instance.exec.return_value.one_or_none.return_value = mock_dataset

        response = client.post("/datasets/test_dataset_001/ingest",
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 400


class TestPatchIngest:
    """Test cases for PATCH /datasets/{dsid}/ingest/{reqid} endpoint."""

    @patch('app.utils.auth_utils.is_admin')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.only_update')
    def test_patch_ingestion_request(self, mock_update, mock_session, mock_admin, client):
        """Test patching ingestion request."""
        mock_admin.return_value = 'admin_user'

        # Mock session
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance

        # Mock updated request
        mock_request = MagicMock()
        mock_update.return_value = mock_request

        patch_data = {"status": "completed"}
        response = client.patch("/datasets/test_dataset_001/ingest/123",
                              json=patch_data,
                              headers={"Authorization": "Bearer admin_token"})

        assert response.status_code == 200


class TestGoogleDriveTransfer:
    """Test cases for POST /datasets/{dsid}/google_drive_transfer endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.rmq_utils.send_pika_message')
    @patch('app.general_utils.get_secret')
    def test_request_drive_transfer(self, mock_secret, mock_send_msg, mock_session, mock_auth_user, client):
        """Test requesting Google Drive transfer."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])
        mock_secret.return_value = 'test_password'

        # Mock session and dataset access
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.all.return_value = [MagicMock()]

        response = client.post("/datasets/test_dataset_001/google_drive_transfer",
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200
        mock_send_msg.assert_called_once()


class TestPatchGoogleDriveTransfer:
    """Test cases for PATCH /datasets/{dsid}/google_drive_transfer/{reqid} endpoint."""

    @patch('app.utils.auth_utils.is_admin')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.only_update')
    def test_patch_transfer_request(self, mock_update, mock_session, mock_admin, client):
        """Test patching transfer request."""
        mock_admin.return_value = 'admin_user'

        # Mock session
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance

        # Mock updated request
        mock_request = MagicMock()
        mock_update.return_value = mock_request

        patch_data = {"status": "completed"}
        response = client.patch("/datasets/test_dataset_001/google_drive_transfer/123",
                              json=patch_data,
                              headers={"Authorization": "Bearer admin_token"})

        assert response.status_code == 200


class TestSciCatUpdate:
    """Test cases for POST /datasets/{dsid}/scicat_update endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.rmq_utils.send_pika_message')
    @patch('app.general_utils.get_secret')
    def test_request_scicat_update(self, mock_secret, mock_send_msg, mock_session, mock_auth_user, client):
        """Test requesting SciCat update."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])
        mock_secret.return_value = 'test_password'

        # Mock session and dataset access
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.all.return_value = [MagicMock()]

        response = client.post("/datasets/test_dataset_001/scicat_update",
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200
        mock_send_msg.assert_called_once()


class TestPatchSciCatUpdate:
    """Test cases for PATCH /datasets/{dsid}/scicat_update/{reqid} endpoint."""

    @patch('app.utils.auth_utils.is_admin')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.only_update')
    def test_patch_scicat_request(self, mock_update, mock_session, mock_admin, client):
        """Test patching SciCat request."""
        mock_admin.return_value = 'admin_user'

        # Mock session
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance

        # Mock updated request
        mock_request = MagicMock()
        mock_update.return_value = mock_request

        patch_data = {"status": "completed"}
        response = client.patch("/datasets/test_dataset_001/scicat_update/123",
                              json=patch_data,
                              headers={"Authorization": "Bearer admin_token"})

        assert response.status_code == 200


class TestScientificMetadata:
    """Test cases for POST and PATCH /datasets/{dsid}/scientific_metadata endpoints."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    def test_add_scientific_metadata(self, mock_add_update, mock_session, mock_auth_user, client):
        """Test adding scientific metadata."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session and dataset
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.one_or_none.return_value = MagicMock()

        # Mock metadata object
        mock_metadata = MagicMock()
        mock_add_update.return_value = mock_metadata

        metadata = {"temperature": 25.5, "pressure": 101.3}
        response = client.post("/datasets/test_dataset_001/scientific_metadata",
                             json=metadata,
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    def test_update_scientific_metadata(self, mock_add_update, mock_session, mock_auth_user, client):
        """Test updating scientific metadata."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session and dataset
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.one_or_none.return_value = MagicMock()

        # Mock existing metadata
        mock_existing = MagicMock()
        mock_existing.scientific_metadata = {"temperature": 20.0}
        mock_session_instance.exec.return_value.one_or_none.side_effect = [MagicMock(), mock_existing]

        # Mock updated metadata
        mock_metadata = MagicMock()
        mock_add_update.return_value = mock_metadata

        metadata_updates = {"pressure": 102.1}
        response = client.patch("/datasets/test_dataset_001/scientific_metadata",
                              json=metadata_updates,
                              headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200


class TestThumbnails:
    """Test cases for POST /datasets/{dsid}/thumbnails endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    def test_add_thumbnail(self, mock_add_update, mock_session, mock_auth_user, client):
        """Test adding thumbnail to dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session and dataset
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_dataset = MagicMock()
        mock_dataset.thumbnails = []
        mock_session_instance.exec.return_value.one_or_none.return_value = mock_dataset

        # Mock thumbnail object
        mock_thumbnail = MagicMock()
        mock_add_update.return_value = mock_thumbnail

        thumbnail_data = {
            "thumbnail_name": "test_thumb.jpg",
            "thumbnail_b64str": "base64encodedstring"
        }
        response = client.post("/datasets/test_dataset_001/thumbnails",
                             json=thumbnail_data,
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200


class TestAssociatedFiles:
    """Test cases for POST /datasets/{dsid}/associated_files endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    def test_add_associated_file(self, mock_add_update, mock_session, mock_auth_user, client):
        """Test adding associated file to dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session and dataset
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_dataset = MagicMock()
        mock_dataset.associated_files = []
        mock_session_instance.exec.return_value.one_or_none.return_value = mock_dataset

        # Mock file object
        mock_file = MagicMock()
        mock_add_update.return_value = mock_file

        file_data = {
            "filename": "data.csv",
            "size": 1024,
            "sha256_hash": "abcd1234"
        }
        response = client.post("/datasets/test_dataset_001/associated_files",
                             json=file_data,
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200


class TestKeywords:
    """Test cases for POST /datasets/{dsid}/keywords endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    def test_add_keyword(self, mock_add_update, mock_session, mock_auth_user, client):
        """Test adding keyword to dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session and dataset
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_dataset = MagicMock()
        mock_dataset.keywords = []
        mock_session_instance.exec.return_value.one_or_none.return_value = mock_dataset

        # Mock keyword and count query
        mock_keyword = MagicMock()
        mock_add_update.return_value = mock_keyword
        mock_session_instance.exec.return_value.one.return_value = 1

        response = client.post("/datasets/test_dataset_001/keywords?keyword=test_keyword",
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200


class TestDriveLocation:
    """Test cases for POST /datasets/{dsid}/drive_location endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    @patch('app.utils.sql_utils.add_or_update')
    @patch('app.utils.sql_utils.table_obj_to_dict')
    def test_add_drive_location(self, mock_to_dict, mock_add_update, mock_session, mock_auth_user, client):
        """Test adding drive location to dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])
        mock_to_dict.return_value = {"drive_id": "test_drive", "folder_id": "test_folder"}

        # Mock session and dataset access
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.all.return_value = [MagicMock()]

        # Mock drive location
        mock_location = MagicMock()
        mock_add_update.return_value = mock_location

        location_data = {
            "drive_id": "test_drive_123",
            "drive_name": "Test Drive",
            "folder_id": "test_folder_456"
        }
        response = client.post("/datasets/test_dataset_001/drive_location",
                             json=location_data,
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200


class TestAccessGroups:
    """Test cases for POST /datasets/{dsid}/access_groups endpoint."""

    @patch('app.utils.auth_utils.is_admin')
    @patch('app.api.main.Session')
    @patch('app.api.main.sql_create_ds_acl')
    def test_create_dataset_acl(self, mock_create_acl, mock_session, mock_admin, client):
        """Test creating dataset access control."""
        mock_admin.return_value = 'admin_user'

        # Mock session and objects
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.one_or_none.return_value = MagicMock()

        # Mock ACL creation
        mock_acl = MagicMock()
        mock_create_acl.return_value = mock_acl

        response = client.post("/datasets/test_dataset_001/access_groups?group_name=test_group&read=true&write=false",
                             headers={"Authorization": "Bearer admin_token"})

        assert response.status_code == 200


class TestSamples:
    """Test cases for POST /datasets/{dsid}/samples/{sample_id} endpoint."""

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    def test_add_sample_to_dataset(self, mock_session, mock_auth_user, client):
        """Test adding sample to dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session, dataset, and sample
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance

        mock_dataset = MagicMock()
        mock_dataset.id = 1
        mock_dataset.unique_id = "test_dataset_001"
        mock_dataset.dataset_name = "Test Dataset"

        mock_sample = MagicMock()
        mock_sample.id = 1
        mock_sample.unique_id = "test_sample_001"
        mock_sample.sample_name = "Test Sample"

        mock_session_instance.exec.return_value.one_or_none.side_effect = [mock_dataset, mock_sample]

        response = client.post("/datasets/test_dataset_001/samples/test_sample_001",
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["dataset_id"] == "test_dataset_001"
        assert response_data["sample_id"] == "test_sample_001"

    @patch('app.utils.auth_utils.authorize_user')
    @patch('app.api.main.Session')
    def test_add_sample_to_dataset_not_found(self, mock_session, mock_auth_user, client):
        """Test adding sample to non-existent dataset."""
        mock_auth_user.return_value = ('test_user', [1, 2, 3])

        # Mock session with no dataset found
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance
        mock_session_instance.exec.return_value.one_or_none.return_value = None

        response = client.post("/datasets/nonexistent_dataset/samples/test_sample_001",
                             headers={"Authorization": "Bearer test_token"})

        assert response.status_code == 404