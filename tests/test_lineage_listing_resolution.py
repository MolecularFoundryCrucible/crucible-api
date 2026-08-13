"""Parent/child lineage listing endpoints -- GET /datasets/{id}/parents,
/children, /datasets/{id}/samples, and the sample equivalents -- returned
raw ORM rows straight through DatasetRead/SampleRead.

For datasets this was a live crash risk, not just staleness: DatasetRead.
instrument_id is the instrument's resolved slug (str), but Dataset.
instrument_id is the legacy int FK -- the exact bug already fixed for
get_resource/get_sample/list_samples (see
test_dataset_instrument_id_serialization.py). Any parent/child dataset with
a registered instrument would 500.

project_id on all five was separately stale after a project rename (see
test_project_rename_resolves_live.py) since it echoed the text column
instead of resolving live from project_mfid.
"""
from sqlmodel import Session

from app.models.core_models import Dataset, DatasetLink, DatasetSampleLink, Instrument, Project, Resource, Sample, SampleLink, User
from app.utils.permissions import PlatformRole


def _setup(db_engine):
    with Session(db_engine) as session:
        user = User(first_name="A", last_name="B", email="a@x.com", unique_id="0000-0001")
        session.add(user)
        inst = Instrument(instrument_id="xrd-1", instrument_name="xrd-1", owner="facility", location="b30")
        session.add(inst)
        proj = Project(project_id="proj_old", organization="LBNL", project_lead_orcid="0000-0001")
        session.add(proj)
        session.commit()
        session.refresh(inst)
        session.refresh(proj)
        mfid = proj.unique_id

        parent_ds = Dataset(unique_id="ds_parent", dataset_name="parent", owner_orcid="0000-0001",
                            instrument_id=inst.id, instrument_mfid=inst.unique_id,
                            project_id="proj_old", project_mfid=mfid)
        child_ds = Dataset(unique_id="ds_child", dataset_name="child", owner_orcid="0000-0001",
                           instrument_id=inst.id, instrument_mfid=inst.unique_id,
                           project_id="proj_old", project_mfid=mfid)
        parent_samp = Sample(unique_id="samp_parent", sample_name="parent", owner_orcid="0000-0001",
                             project_id="proj_old", project_mfid=mfid)
        child_samp = Sample(unique_id="samp_child", sample_name="child", owner_orcid="0000-0001",
                            project_id="proj_old", project_mfid=mfid)
        session.add(parent_ds)
        session.add(child_ds)
        session.add(parent_samp)
        session.add(child_samp)
        session.add(Resource(mfid="ds_parent", resource_type="dataset"))
        session.add(Resource(mfid="ds_child", resource_type="dataset"))
        session.add(Resource(mfid="samp_parent", resource_type="sample"))
        session.add(Resource(mfid="samp_child", resource_type="sample"))
        session.commit()
        session.refresh(parent_ds)
        session.refresh(child_ds)
        session.refresh(parent_samp)
        session.refresh(child_samp)

        session.add(DatasetLink(parent_dataset_id=parent_ds.id, child_dataset_id=child_ds.id))
        session.add(SampleLink(parent_sample_id=parent_samp.id, sample_id=child_samp.id))
        session.add(DatasetSampleLink(sample_id=parent_samp.id, dataset_id=parent_ds.id))
        session.commit()

        # Rename after everything is linked -- stored project_id text on
        # every row above is now stale, matching the real-world scenario.
        proj.project_id = "proj_new"
        session.add(proj)
        session.commit()
        return inst.instrument_id


def test_list_parent_datasets_resolves_instrument_and_project(client, db_engine, auth_as):
    slug = _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/datasets/ds_child/parents")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["instrument_id"] == slug
    assert item["project_id"] == "proj_new"


def test_list_child_datasets_resolves_instrument_and_project(client, db_engine, auth_as):
    slug = _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/datasets/ds_parent/children")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["instrument_id"] == slug
    assert item["project_id"] == "proj_new"


def test_read_samples_in_dataset_resolves_project(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/datasets/ds_parent/samples")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["project_id"] == "proj_new"


def test_list_parent_samples_resolves_project(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/samples/samp_child/parents")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["project_id"] == "proj_new"


def test_list_child_samples_resolves_project(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/samples/samp_parent/children")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["project_id"] == "proj_new"
