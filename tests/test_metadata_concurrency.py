import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

from app.api.routes import resources as resources_routes
from app.models.core_models import Keyword, Resource, ResourceEmbedding, ScientificMetadataBinary
from app.utils.permissions import PlatformRole, Principal
from tests.conftest import TEST_TABLES


@pytest.fixture
def pg_engine():
    url = os.getenv("CRUCIBLE_TEST_PG_URL")
    if not url:
        pytest.skip("requires isolated test PostgreSQL")
    admin = create_engine(url)
    schema = "test_metadata_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        SQLModel.metadata.create_all(engine, tables=[table for table in TEST_TABLES if table is not ResourceEmbedding.__table__] + [Keyword.__table__, ScientificMetadataBinary.__table__])
        with Session(engine) as session:
            session.add(Resource(mfid="r1", resource_type="dataset"))
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("first_exists", [False, True])
def test_concurrent_metadata_patches_keep_both_updates(pg_engine, monkeypatch, first_exists):
    if first_exists:
        with Session(pg_engine) as session:
            session.add(ScientificMetadataBinary(unique_id="r1", scientific_metadata={"base": 1}))
            session.commit()
    monkeypatch.setattr(resources_routes, "engine", pg_engine)
    original = resources_routes.add_or_update

    def slow_add_or_update(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)
    monkeypatch.setattr(resources_routes, "add_or_update", slow_add_or_update)
    admin = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    def patch(update):
        resources_routes.update_resource_metadata(admin, "r1", update, BackgroundTasks())

    with ThreadPoolExecutor(2) as pool:
        for future in [pool.submit(patch, {"a": 1}), pool.submit(patch, {"b": 2})]:
            future.result()

    with Session(pg_engine) as session:
        stored = session.exec(select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == "r1")).all()
    assert len(stored) == 1
    assert stored[0].scientific_metadata == ({"base": 1} if first_exists else {}) | {"a": 1, "b": 2}


def test_deletion_request_and_new_dataset_cannot_both_succeed(pg_engine, monkeypatch, auth_as):
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.api.routes import datasets as datasets_routes
    from app.models.core_models import AccessGroup, Dataset, Project, User
    from app.utils.permissions import AccessGroupType
    with Session(pg_engine) as session:
        session.add_all([User(unique_id="lead", first_name="L", last_name="D", email=None), User(unique_id="admin", first_name="A", last_name="D", email=None)])
        session.flush()
        project = Project(project_id="racing", organization="MF", project_lead_orcid="lead")
        session.add(project)
        session.flush()
        session.add_all([Resource(mfid=project.unique_id, resource_type="project"),
                         AccessGroup(group_name=project.unique_id, group_type=AccessGroupType.PROJECT.value),
                         AccessGroup(group_name="admin", group_type=AccessGroupType.IDENTITY.value)])
        session.commit()
        project_mfid = project.unique_id
    for module in (resources_routes, datasets_routes):
        monkeypatch.setattr(module, "engine", pg_engine)
    original = resources_routes.deletion_summary

    def slow_summary(*args, **kwargs):
        summary = original(*args, **kwargs)
        time.sleep(0.5)
        return summary
    monkeypatch.setattr(resources_routes, "deletion_summary", slow_summary)
    auth_as("admin", platform_role=PlatformRole.ADMIN)
    client = TestClient(app)

    def request_deletion():
        return client.post(f"/resources/{project_mfid}/delete", params={"reason": "x"}).status_code

    def create_dataset():
        time.sleep(0.15)
        response = client.post("/datasets", json={"dataset_name": "late", "project_mfid": project_mfid})
        return response.status_code, response.json().get("detail")

    with ThreadPoolExecutor(2) as pool:
        deletion, creation = (future.result() for future in [pool.submit(request_deletion), pool.submit(create_dataset)])

    with Session(pg_engine) as session:
        assigned = session.exec(select(Dataset).where(Dataset.project_mfid == project_mfid)).all()
    assert deletion == 200
    assert creation[0] == 409 and creation[1]["code"] == "pending_deletion", creation
    assert assigned == []


def test_metadata_search_member_affiliation_narrows_only_datasets_and_samples(pg_engine, monkeypatch):
    from app.models.core_models import AccessGroup, Dataset, Instrument, ResourceACL, Sample
    from app.models.query_params import ResourceAffiliation
    from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType, GroupMembership, Role
    with Session(pg_engine) as session:
        public = AccessGroup(group_name=PUBLIC_GROUP_NAME, group_type=AccessGroupType.SYSTEM.value)
        project = AccessGroup(group_name="my-project", group_type=AccessGroupType.PROJECT.value)
        session.add_all([public, project])
        session.flush()
        records = {"ds-public-only": "dataset", "ds-public-mine": "dataset", "ds-mine": "dataset", "sm-public-only": "sample", "inst-public": "instrument"}
        for mfid, kind in records.items():
            session.add(Resource(mfid=mfid, resource_type=kind))
            if kind == "dataset":
                session.add(Dataset(unique_id=mfid, dataset_name=mfid))
            elif kind == "sample":
                session.add(Sample(unique_id=mfid, sample_name=mfid))
            else:
                session.add(Instrument(unique_id=mfid, instrument_id=mfid, instrument_name=mfid, owner="0000-0008-0000-0009", location="lab", manufacturer="m", model="m", other_id="o", other_id_source="o", description="d", instrument_type="t"))
            session.add(ScientificMetadataBinary(unique_id=mfid, scientific_metadata={"material": "perovskite"}))
            if "public" in mfid:
                session.add(ResourceACL(mfid=mfid, accessgroup_id=public.id, read=True, write=False, permission="viewer"))
            if "mine" in mfid:
                session.add(ResourceACL(mfid=mfid, accessgroup_id=project.id, read=True, write=True, permission="admin"))
        session.commit()
        caller = Principal(user_id="0000-0008-0000-0001", groups=(
            GroupMembership(id=public.id, group_name=PUBLIC_GROUP_NAME, role=Role.VIEWER),
            GroupMembership(id=project.id, group_name="my-project", role=Role.EDITOR),
        ))
    monkeypatch.setattr(resources_routes, "engine", pg_engine)

    def found(affiliation):
        return sorted(item.unique_id for item in resources_routes.search_resource_metadata_v2(caller, "perovskite", 20, affiliation).items)

    assert found(None) == sorted(records)
    assert found([ResourceAffiliation.MEMBER]) == ["ds-mine", "ds-public-mine", "inst-public"]
