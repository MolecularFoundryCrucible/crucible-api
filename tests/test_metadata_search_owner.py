import pytest
from sqlmodel import Session

import app.api.routes.resources as resources_routes
from app.tables import (
    AccessGroup, Dataset, Instrument, Project, Resource, Sample, ScientificMetadataBinary, User, UserAccessGroupLink,
)
from app.utils.permissions import PlatformRole, Principal
from tests.pg_fixture import PG_URL, throwaway_database

pytestmark = pytest.mark.skipif(not PG_URL, reason="CRUCIBLE_TEST_PG_URL not set")

ADMIN = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)


def _seed(engine):
    with Session(engine) as session:
        def add(row):
            session.add(row)
            session.flush()
            return row

        people = {name: add(User(unique_id=name, username=name, first_name="T", last_name=name)) for name in ("alice", "bob")}
        for owner, mfid, model in (
            ("alice", "ds-a", Dataset), ("bob", "ds-b", Dataset),
            ("alice", "sa-a", Sample), ("bob", "inst-b", Instrument), ("alice", "proj-a", Project), ("bob", "proj-b", Project),
        ):
            kind = {Dataset: "dataset", Sample: "sample", Instrument: "instrument", Project: "project"}[model]
            add(Resource(mfid=mfid, resource_type=kind))
            if model is Dataset:
                add(Dataset(unique_id=mfid, dataset_name=mfid, owner_user_id=people[owner].id))
            elif model is Sample:
                add(Sample(unique_id=mfid, sample_name=mfid, owner_user_id=people[owner].id))
            elif model is Instrument:
                add(Instrument(unique_id=mfid, instrument_id=mfid, instrument_name=mfid, location="b30", owner_user_id=people[owner].id))
            else:
                add(Project(unique_id=mfid, project_id=mfid, organization="MF", title=mfid))
                group = add(AccessGroup(group_name=mfid, group_type="project"))
                add(UserAccessGroupLink(user_id=people[owner].id, accessgroup_id=group.id, role="owner"))
            add(ScientificMetadataBinary(unique_id=mfid, scientific_metadata={"note": "perovskite film"}))
        session.commit()


def test_metadata_search_filters_by_owner_across_record_types(monkeypatch):
    from app.cli.db import upgrade_database

    with throwaway_database() as engine:
        upgrade_database(engine)
        _seed(engine)
        monkeypatch.setattr(resources_routes, "get_engine", lambda: engine)

        def search(owner_ref):
            page = resources_routes.search_resource_metadata_v2(auth_res=ADMIN, q="perovskite", limit=20, affiliation=None, owner_ref=owner_ref)
            return sorted(item.unique_id for item in page.items)

        assert search(None) == ["ds-a", "ds-b", "inst-b", "proj-a", "proj-b", "sa-a"]
        assert search(["alice"]) == ["ds-a", "proj-a", "sa-a"]
        assert search(["bob", "alice"]) == ["ds-a", "ds-b", "inst-b", "proj-a", "proj-b", "sa-a"]
        assert search(["nobody"]) == []
