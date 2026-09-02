"""DatasetRead/DatasetResponse.instrument_id is the instrument's validated
slug (a str) resolved server-side from Dataset.instrument_mfid -- not
Dataset.instrument_id, which is a legacy internal int FK to Instrument.id.
Passing a Dataset ORM row straight to model_validate() without resolving
that slug crashes with a Pydantic string_type error the moment
instrument_id is non-null, since ORM attribute access returns the raw int.

This broke in production for GET /resources/{sample_mfid} on any sample
with an associated dataset that had a registered instrument -- no test
caught it because nothing exercised these read paths with a non-null
instrument reference. Covers every read path that serializes a Dataset
nested under a Sample response: GET /resources/{mfid}, GET /samples/{id},
GET /samples (list).
"""
from sqlmodel import Session, select

from app.models.core_models import Dataset, DatasetSampleLink, Instrument, Resource, Sample, User
from app.utils.permissions import PlatformRole


def _setup(db_engine):
    with Session(db_engine) as session:
        user = User(first_name="A", last_name="B", email="a@x.com", unique_id="0000-0001")
        session.add(user)
        inst = Instrument(instrument_id="xrd-1", instrument_name="xrd-1", owner="facility", location="b30")
        session.add(inst)
        session.commit()
        session.refresh(inst)

        samp = Sample(unique_id="samp_1", sample_name="s1", owner_orcid="0000-0001")
        ds = Dataset(unique_id="ds_1", dataset_name="d1", owner_orcid="0000-0001",
                    instrument_id=inst.id, instrument_mfid=inst.unique_id,
                    instrument_name=inst.instrument_name)
        session.add(samp)
        session.add(ds)
        session.add(Resource(mfid="samp_1", resource_type="sample"))
        session.add(Resource(mfid="ds_1", resource_type="dataset"))
        session.commit()
        session.refresh(samp)
        session.refresh(ds)
        session.add(DatasetSampleLink(sample_id=samp.id, dataset_id=ds.id))
        session.commit()
        return inst.instrument_id


def test_get_resource_resolves_instrument_id_slug(client, db_engine, auth_as):
    slug = _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/resources/samp_1")
    assert resp.status_code == 200, resp.text
    datasets = resp.json()["datasets"]
    assert len(datasets) == 1
    assert datasets[0]["instrument_id"] == slug


def test_get_sample_resolves_instrument_id_slug(client, db_engine, auth_as):
    slug = _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/samples/samp_1")
    assert resp.status_code == 200, resp.text
    datasets = resp.json()["datasets"]
    assert len(datasets) == 1
    assert datasets[0]["instrument_id"] == slug


def test_list_samples_resolves_instrument_id_slug(client, db_engine, auth_as):
    slug = _setup(db_engine)
    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)

    resp = client.get("/samples", params={"include_links": True, "include_datasets": True})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    sample = next(s for s in items if s["unique_id"] == "samp_1")
    assert sample["datasets"][0]["instrument_id"] == slug
