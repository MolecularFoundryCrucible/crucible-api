import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

from app.models.core_models import AccessGroup, Dataset, Keyword, Resource, ResourceACL, UploadSession, User, UserAccessGroupLink
from app.models.multipart import MultipartInitiate
from app.services.multipart import initiate, reserve
from app.utils.auth_utils import load_principal
from app.utils.multipart_config import MultipartStorageConfig
from tests.conftest import TEST_TABLES


@pytest.fixture
def pg_upload_engine():
    url = os.getenv("CRUCIBLE_TEST_PG_URL")
    if not url:
        pytest.skip("requires isolated test PostgreSQL")
    admin = create_engine(url)
    schema = "test_multipart_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        SQLModel.metadata.create_all(engine, tables=TEST_TABLES + [Keyword.__table__], checkfirst=False)
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE upload_sessions ALTER COLUMN expires_at TYPE timestamptz USING expires_at::timestamptz"))
        with Session(engine) as session:
            user = User(unique_id="owner", first_name="A", last_name="B", email="a@example.test")
            group = AccessGroup(group_name="owner-group")
            session.add_all([user, group, Resource(mfid="ds", resource_type="dataset"), Dataset(unique_id="ds", dataset_name="data")])
            session.commit()
            session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=group.id, role="owner"))
            session.add(ResourceACL(mfid="ds", accessgroup_id=group.id, read=True, write=True, permission="owner"))
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


class SerialStorage:
    def __init__(self):
        self.lock = Lock()
        self.created = []

    def open_uploads(self, bucket, key):
        with self.lock:
            return list(self.created)

    def start_upload(self, bucket, key):
        with self.lock:
            self.created.append(f"provider-{len(self.created) + 1}")
            return self.created[-1]


def test_parallel_initiation_with_one_key_returns_one_session(pg_upload_engine):
    engine = pg_upload_engine
    config = MultipartStorageConfig(enabled=True, bucket="dedicated", project="test", signer_service_account="signer")
    storage = SerialStorage()
    with Session(engine) as session:
        principal = load_principal(session, "owner")
    barrier = Barrier(2)

    def begin():
        barrier.wait(timeout=5)
        return initiate(engine, lambda: storage, principal, "ds", MultipartInitiate(filename="data", size=1), "same-key", config)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: begin(), range(2)))
    assert results[0].upload_id == results[1].upload_id
    assert {result.state for result in results} == {"uploading"}
    with Session(engine) as session:
        upload = session.exec(select(UploadSession)).one()
        assert upload.provider_upload_id in storage.created


def test_parallel_filename_reservation_and_owner_quota(pg_upload_engine):
    from fastapi import HTTPException
    engine = pg_upload_engine
    config = replace(MultipartStorageConfig(), enabled=True, bucket="dedicated", owner_sessions=1)
    with Session(engine) as session:
        principal = load_principal(session, "owner")
    barrier = Barrier(2)

    def begin(index):
        barrier.wait(timeout=5)
        try:
            reserve(engine, principal, "ds", MultipartInitiate(filename=f"data{index}", size=1), None, config)
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(begin, range(2))) == [200, 429]
    with Session(engine) as session:
        assert len(session.exec(select(UploadSession)).all()) == 1
