from unittest.mock import patch
import pytest
from pydantic import ValidationError
from sqlmodel import Session, select
from app.utils.permissions import PUBLIC_GROUP_NAME, PlatformRole, Role
from app.models.core_models import Resource, Dataset, AssociatedFile, AccessGroup, ResourceACL
from app.models.datasets import AssociatedFileCreate, AssociatedFileRead, AssociatedFileUpdate


def grant_public(session, mfid):
    """Make a resource publicly readable and return the public group's id.

    Public is an ACL grant against the virtual group now, not just the
    Resource.public boolean. The caller also needs the group id: load_principal
    injects public-group membership into every principal in production, but the
    auth_as fixture does not.
    """
    pub = session.exec(
        select(AccessGroup).where(AccessGroup.group_name == PUBLIC_GROUP_NAME)
    ).one_or_none()
    if pub is None:
        pub = AccessGroup(group_name=PUBLIC_GROUP_NAME)
        session.add(pub)
        session.commit()
        session.refresh(pub)
    session.add(ResourceACL(mfid=mfid, accessgroup_id=pub.id,
                            read=True, write=False, permission="viewer"))
    session.commit()
    return pub.id


def _make_public_dataset_with_file(db_engine, dsid, storage_path="mf-storage-prod/x/f.txt"):
    """Returns (file_mfid, public_group_id)."""
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
        return af.mfid, grant_public(session, dsid)


def test_gcs_backend_download_link_still_calls_generate_signed_url(client, db_engine, auth_as):
    file_mfid, public_gid = _make_public_dataset_with_file(db_engine, "ds_pub_1")
    auth_as("0000-0000-0000-0001", groups=[(PUBLIC_GROUP_NAME, public_gid, Role.VIEWER)])

    with patch(
        "app.api.routes.files._signed_download",
        return_value=("https://signed.example/x", 3600),
    ) as mock_sign:
        resp = client.get(f"/files/{file_mfid}/download_link")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": "https://signed.example/x", "expires_in": 3600}
    mock_sign.assert_called_once()


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


def test_associated_file_create_schema_defaults_and_overrides():
    default = AssociatedFileCreate(filename="a.txt", size=1, sha256_hash="h1")
    assert default.storage_backend == "gcs"
    assert default.access_note is None

    remote = AssociatedFileCreate(
        filename="b.txt", size=2, sha256_hash="h2",
        storage_backend="lawrencium",
        storage_path="/global/cfs/cdirs/project/b.txt",
        access_note="ask PI for access",
    )
    assert remote.storage_backend == "lawrencium"
    assert remote.storage_path == "/global/cfs/cdirs/project/b.txt"
    assert remote.access_note == "ask PI for access"

    with pytest.raises(ValidationError, match="storage_path is assigned by the upload API"):
        AssociatedFileCreate(
            filename="c.txt",
            size=3,
            sha256_hash="h3",
            storage_path="bucket/forged.txt",
        )


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


def _grant_write_access(db_engine, dsid, user_id):
    with Session(db_engine) as session:
        session.add(Resource(mfid=dsid, resource_type="dataset", public=False))
        session.add(Dataset(unique_id=dsid, dataset_name="test", owner_orcid=user_id))
        ag = AccessGroup(group_name=dsid)
        session.add(ag)
        session.commit()
        session.refresh(ag)
        # role, not just read/write: the checks read the role column now.
        session.add(ResourceACL(mfid=dsid, accessgroup_id=ag.id, read=True, write=True,
                                permission="owner"))
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
        public_gid = grant_public(session, "ds_pub_2")

    auth_as("0000-0000-0000-0001",
            groups=[(PUBLIC_GROUP_NAME, public_gid, Role.VIEWER)])

    with patch("app.api.routes.files._signed_download") as mock_sign, \
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
        # Public is an ACL grant now, not just the boolean.
        pub = AccessGroup(group_name=PUBLIC_GROUP_NAME)
        session.add(pub)
        session.commit()
        session.refresh(pub)
        session.add(ResourceACL(mfid="ds_pub_3", accessgroup_id=pub.id,
                                read=True, write=False, permission="viewer"))
        session.commit()
        public_gid = pub.id
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

    # load_principal injects the public group into every principal; the fixture
    # does not, so a test exercising public access has to say so.
    auth_as("0000-0000-0000-0001", groups=[(PUBLIC_GROUP_NAME, public_gid, Role.VIEWER)])

    with patch(
        "app.api.routes.datasets._signed_download",
        return_value=("https://signed.example/local.txt", 3600),
    ):
        resp = client.get("/datasets/ds_pub_3/download_links")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body[gcs_mfid] == "https://signed.example/local.txt"
    assert body[remote_mfid] is None


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
        grant_public(session, "ds_pub_4")

    auth_as("0000-0000-0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.post(f"/files/{file_mfid}/ingest")

    assert resp.status_code == 400, resp.text
    assert "globus" in resp.json()["detail"]


def test_viewer_cannot_request_ingestion(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_ingest_viewer", resource_type="dataset", public=False))
        ds = Dataset(unique_id="ds_ingest_viewer", dataset_name="test", owner_orcid="0000-0000-0000-0001")
        group = AccessGroup(group_name="ingest-viewers")
        session.add_all([ds, group])
        session.commit()
        session.refresh(ds)
        session.refresh(group)
        session.add(ResourceACL(
            mfid=ds.unique_id,
            accessgroup_id=group.id,
            read=True,
            write=False,
            permission=Role.VIEWER.db_value,
        ))
        af = AssociatedFile(
            filename="remote.dat",
            storage_backend="globus",
            storage_path="globus://endpoint/remote.dat",
            size=1,
            sha256_hash="viewer-ingest",
            dataset_id=ds.id,
        )
        session.add(af)
        session.commit()
        session.refresh(af)
        file_mfid = af.mfid
        group_id = group.id

    auth_as("0000-0000-0000-0001", groups=[("ingest-viewers", group_id, Role.VIEWER)])

    resp = client.post(f"/files/{file_mfid}/ingest")

    assert resp.status_code == 403, resp.text


def test_contributor_can_reach_ingestion_request_validation(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_ingest_contributor", resource_type="dataset", public=False))
        ds = Dataset(unique_id="ds_ingest_contributor", dataset_name="test", owner_orcid="0000-0000-0000-0001")
        group = AccessGroup(group_name="ingest-contributors")
        session.add_all([ds, group])
        session.commit()
        session.refresh(ds)
        session.refresh(group)
        session.add(ResourceACL(
            mfid=ds.unique_id,
            accessgroup_id=group.id,
            read=True,
            write=True,
            permission=Role.CONTRIBUTOR.db_value,
        ))
        af = AssociatedFile(
            filename="remote.dat",
            storage_backend="globus",
            storage_path="globus://endpoint/remote.dat",
            size=1,
            sha256_hash="contributor-ingest",
            dataset_id=ds.id,
        )
        session.add(af)
        session.commit()
        session.refresh(af)
        file_mfid = af.mfid
        group_id = group.id

    auth_as("0000-0000-0000-0001", groups=[("ingest-contributors", group_id, Role.CONTRIBUTOR)])

    resp = client.post(f"/files/{file_mfid}/ingest")

    assert resp.status_code == 400, resp.text
    assert "globus" in resp.json()["detail"]
