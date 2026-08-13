"""A dataset/sample's project_id in API responses must reflect the
project's CURRENT name, not whatever project_id text was stored on it at
creation time -- otherwise renaming a project (see test_project_rename.py)
would leave every dataset/sample created before the rename showing a name
that no longer resolves to anything. resolve_project_mfids_to_ids batch
-resolves project_mfid -> live project_id at every read path.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Dataset, Project, Resource, ResourceACL, Sample, User, UserAccessGroupLink
from app.utils.permissions import PlatformRole


def _setup(db_engine):
    with Session(db_engine) as session:
        user = User(first_name="A", last_name="B", email="a@x.com", unique_id="0000-0001")
        session.add(user)
        proj = Project(project_id="proj_old", organization="LBNL", project_lead_orcid="0000-0001")
        session.add(proj)
        session.commit()
        session.refresh(proj)
        mfid = proj.unique_id

        ag = AccessGroup(group_name=mfid)
        session.add(ag)
        session.add(Resource(mfid=mfid, resource_type="project"))
        session.commit()
        session.refresh(ag)
        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="owner"))
        session.add(ResourceACL(mfid=mfid, accessgroup_id=ag.id, read=True, write=True, permission="owner"))

        ds = Dataset(unique_id="ds_1", dataset_name="ds-one", owner_orcid="0000-0001",
                    project_id="proj_old", project_mfid=mfid)
        samp = Sample(unique_id="samp_1", sample_name="s1", owner_orcid="0000-0001",
                     project_id="proj_old", project_mfid=mfid)
        session.add(ds)
        session.add(samp)
        session.add(Resource(mfid="ds_1", resource_type="dataset"))
        session.add(Resource(mfid="samp_1", resource_type="sample"))
        session.commit()

        # The rename itself -- project_id changes, project_mfid on the
        # dataset/sample rows (and everything keyed by unique_id) does not.
        proj.project_id = "proj_new"
        session.add(proj)
        session.commit()
        return mfid


def test_get_dataset_shows_renamed_project_id(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/datasets/ds_1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == "proj_new"


def test_list_datasets_shows_renamed_project_id(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/datasets")
    assert resp.status_code == 200, resp.text
    ds = next(d for d in resp.json()["items"] if d["unique_id"] == "ds_1")
    assert ds["project_id"] == "proj_new"


def test_get_sample_shows_renamed_project_id(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/samples/samp_1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == "proj_new"


def test_list_samples_shows_renamed_project_id(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/samples")
    assert resp.status_code == 200, resp.text
    samp = next(s for s in resp.json()["items"] if s["unique_id"] == "samp_1")
    assert samp["project_id"] == "proj_new"


def test_get_resource_shows_renamed_project_id_for_dataset_and_sample(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    ds_resp = client.get("/resources/ds_1")
    assert ds_resp.status_code == 200, ds_resp.text
    assert ds_resp.json()["project_id"] == "proj_new"

    samp_resp = client.get("/resources/samp_1")
    assert samp_resp.status_code == 200, samp_resp.text
    assert samp_resp.json()["project_id"] == "proj_new"


# /datasets/search and /samples/search also resolve project_id the same way
# (both the ?project_id= filter and the response field), but the endpoint
# itself uses postgres-only pg_trgm and isn't testable against SQLite --
# same pre-existing gap as every other test in this suite, none of which
# cover /datasets/search or /samples/search either.
