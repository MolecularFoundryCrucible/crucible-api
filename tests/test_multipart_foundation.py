from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.routes import gcs_storage
from app.models.core_models import Dataset, Resource, UploadSession
from app.models.uploads import UploadComplete
from app.utils.deletion_utils import _hard_delete_dataset
from app.utils.multipart_config import MultipartStorageConfig
from app.utils.permissions import Principal


def make_session(**kwargs):
    values = dict(dataset_mfid="ds", gcs_path="uploads/object", associated_file_name="data", owner_user_id="owner", expires_at="2000-01-01T00:00:00+00:00", size=10)
    values.update(kwargs)
    return UploadSession(**values)


@pytest.mark.parametrize("protocol,owner,status", [(None, "owner", 410), ("gcs_xml_v1", "owner", 409), ("future", "owner", 409), ("gcs_xml_v1", "other", 403)])
def test_legacy_completion_protocol_guard(protocol, owner, status):
    upload = make_session(protocol=protocol)
    with patch.object(gcs_storage, "Session") as session_cls, patch.object(gcs_storage, "_get_gcs_storage_client") as storage:
        session = session_cls.return_value.__enter__.return_value
        session.exec.return_value.one_or_none.return_value = upload
        with pytest.raises(HTTPException) as error:
            gcs_storage.complete_resumable_upload(dsid="ds", body=UploadComplete(upload_id=upload.upload_id, sha256_hash="claimed"), auth_res=Principal(user_id=owner, groups=()))
        assert error.value.status_code == status
        storage.assert_not_called()
        if protocol is None:
            session.delete.assert_called_once_with(upload)
        else:
            session.delete.assert_not_called()
            session.commit.assert_not_called()


def test_legacy_inserts_and_new_idempotency(db_engine):
    with Session(db_engine) as session:
        session.add_all([make_session(), make_session()])
        session.commit()
        assert all(row.protocol is None for row in session.exec(select(UploadSession)).all())
        session.add(make_session(protocol="gcs_xml_v1", state="initiating", idempotency_key="key"))
        session.commit()
        session.add(make_session(protocol="gcs_xml_v1", state="initiating", idempotency_key="key"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(make_session(protocol="gcs_xml_v1", state="initiating", idempotency_key="key", owner_user_id="other"))
        session.commit()


@pytest.mark.parametrize("changes", [{"state": None}, {"state": "unknown"}, {"part_count": 10001}, {"part_size": 0}])
def test_new_session_constraints(db_engine, changes):
    with Session(db_engine) as session:
        session.add(make_session(**(dict(protocol="gcs_xml_v1", state="initiating") | changes)))
        with pytest.raises(IntegrityError):
            session.commit()


def test_dataset_deletion_removes_all_upload_sessions_atomically(db_engine):
    with Session(db_engine) as session:
        resource = Resource(mfid="ds", resource_type="dataset")
        session.add(resource)
        session.add(Dataset(unique_id="ds", dataset_name="data"))
        session.add_all([make_session(), make_session(protocol="gcs_xml_v1", state="uploading", provider_upload_id="provider-id")])
        session.commit()
        _hard_delete_dataset("ds", session, resource)
        session.rollback()
        assert len(session.exec(select(UploadSession)).all()) == 2
        _hard_delete_dataset("ds", session, resource)
        session.commit()
        assert session.exec(select(UploadSession)).all() == []


def test_config_defaults_and_explicit_storage():
    assert MultipartStorageConfig.from_env({}) == MultipartStorageConfig()
    with pytest.raises(ValueError):
        MultipartStorageConfig.from_env({"MULTIPART_UPLOAD_ENABLED": "true"})
    with pytest.raises(ValueError):
        MultipartStorageConfig.from_env({"MULTIPART_UPLOAD_ENABLED": "yes"})
    env = dict(MULTIPART_UPLOAD_ENABLED="true", MULTIPART_UPLOAD_BUCKET="dedicated", MULTIPART_UPLOAD_SIGNER="signer@example.iam.gserviceaccount.com", MULTIPART_UPLOAD_PROJECT="project")
    assert MultipartStorageConfig.from_env(env).enabled
    with pytest.raises(ValueError):
        MultipartStorageConfig.from_env(env | {"MULTIPART_UPLOAD_BUCKET": "mf-storage-prod"})
