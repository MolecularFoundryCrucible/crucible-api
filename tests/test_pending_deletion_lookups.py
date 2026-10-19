import pytest
from sqlmodel import Session

from app.models.core_models import AccessGroup, Dataset, DatasetSampleLink, DeletionRequest, Resource, ResourceACL, Sample, SampleLink, User
from app.utils.permissions import Role

ME = "0000-0009-0000-0001"


@pytest.fixture
def records(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(User(first_name="P", last_name="D", email=None, unique_id=ME, username="pending"))
        session.add(AccessGroup(id=1, group_name="readers"))
        entities = [Dataset(unique_id=m, dataset_name=m) for m in ("ds", "ds-pending")] + [Sample(unique_id=m, sample_name=m) for m in ("sm", "sm-pending", "sm-parent")]
        session.add_all(entities)
        session.commit()
        for entity in entities:
            session.refresh(entity)
            session.add(Resource(mfid=entity.unique_id, resource_type="dataset" if isinstance(entity, Dataset) else "sample"))
            session.add(ResourceACL(mfid=entity.unique_id, accessgroup_id=1, read=True, write=False, permission=Role.VIEWER.db_value))
        by_mfid = {e.unique_id: e.id for e in entities}
        for sample in ("sm", "sm-pending"):
            session.add(SampleLink(parent_sample_id=by_mfid["sm-parent"], sample_id=by_mfid[sample]))
            for dataset in ("ds", "ds-pending"):
                session.add(DatasetSampleLink(sample_id=by_mfid[sample], dataset_id=by_mfid[dataset]))
        for mfid in ("ds-pending", "sm-pending"):
            session.add(DeletionRequest(resource_id=mfid, resource_type="dataset" if mfid.startswith("ds") else "sample", requester_id=ME, status="pending"))
        session.commit()
    auth_as(ME, groups=[("readers", 1, Role.VIEWER)])
    return client


@pytest.mark.parametrize("path, params", [
    ("/samples", {"dataset_mfid": "{ds}"}),
    ("/samples/facets", {"dataset_mfid": "{ds}", "field": "project"}),
    ("/datasets/facets", {"sample_mfid": "{sm}", "field": "project"}),
    ("/samples/{sm}/datasets", {}),
    ("/samples/{sm}/parents", {}),
    (f"/users/{ME}/datasets/{{ds}}", {}),
])
def test_records_pending_deletion_are_not_found(records, path, params):
    def fill(value, suffix):
        return value.replace("{ds}", "ds" + suffix).replace("{sm}", "sm" + suffix)
    active = records.get(fill(path, ""), params={k: fill(v, "") for k, v in params.items()})
    assert active.status_code == 200, active.text
    pending = records.get(fill(path, "-pending"), params={k: fill(v, "-pending") for k, v in params.items()})
    assert pending.status_code == 404, pending.text
