from contextlib import contextmanager
from unittest.mock import MagicMock

from sqlalchemy import event
from sqlmodel import Session

import app.api.routes.join_requests as join_requests_routes
import app.api.routes.deletion as deletion_routes
import app.api.routes.files as files_routes
from app.models.core_models import (
    AccessGroup,
    AccessGroupJoinRequest,
    AssociatedFile,
    Dataset,
    DeletionRequest,
    Project,
    Resource,
    ResourceACL,
    Sample,
    User,
)
from app.models.join_requests import JoinRequestScope, JoinRequestSort
from app.models.query_params import SortDirection
from app.utils.permissions import AccessGroupType, GroupMembership, PlatformRole, Principal, Role


@contextmanager
def _statement_count(engine):
    statements = []

    def record_statement(*args):
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


def test_join_request_collection_uses_two_queries(db_engine):
    with Session(db_engine) as session:
        requester = User(
            first_name="Join",
            last_name="Requester",
            unique_id="join-requester",
        )
        project = Project(
            project_id="join-project",
            organization="LBNL",
            project_lead_orcid="join-requester",
        )
        session.add_all([requester, project])
        session.commit()
        session.refresh(project)
        group = AccessGroup(
            group_name=project.unique_id,
            group_type=AccessGroupType.PROJECT.value,
        )
        session.add(group)
        session.commit()
        session.refresh(group)
        session.add(AccessGroupJoinRequest(
            accessgroup_id=group.id,
            requester_id=requester.unique_id,
        ))
        session.commit()

    principal = Principal(
        user_id="platform-admin",
        platform_role=PlatformRole.ADMIN,
    )
    with _statement_count(db_engine) as statements, Session(db_engine) as session:
        response = join_requests_routes.list_join_requests_page(
            session,
            principal,
            join_requests_routes.join_request_filters(
                scope=JoinRequestScope.ACCESSIBLE, status=None, project_mfid=None, group_name=None,
                requester_id=None, reviewer_id=None, requested_after=None, requested_before=None,
                reviewed_after=None, reviewed_before=None, q=None,
            ),
            JoinRequestSort.REQUEST_TIME, SortDirection.DESC, 100, None, 0,
        )

    assert len(statements) == 2
    assert response.items[0].group_name == "join-project"


def test_deletion_request_collection_uses_two_queries(db_engine):
    with Session(db_engine) as session:
        session.add_all([
            Dataset(
                unique_id="deletion-dataset",
                dataset_name="Dataset",
                project_id="dataset-project",
            ),
            Sample(
                unique_id="deletion-sample",
                sample_name="Sample",
                project_id="sample-project",
            ),
            Resource(mfid="deletion-dataset", resource_type="dataset"),
            Resource(mfid="deletion-sample", resource_type="sample"),
            DeletionRequest(
                resource_id="deletion-dataset",
                requester_id="requester",
                status="pending",
            ),
            DeletionRequest(
                resource_id="deletion-sample",
                requester_id="requester",
                status="pending",
            ),
        ])
        session.commit()

    principal = Principal(
        user_id="platform-admin",
        platform_role=PlatformRole.ADMIN,
    )
    with _statement_count(db_engine) as statements:
        response = deletion_routes.list_deletion_requests(
            principal,
            status=None,
            limit=100,
            offset=0,
        )

    assert len(statements) == 2
    assert {item.project_id for item in response.items} == {
        "dataset-project",
        "sample-project",
    }


def test_file_detail_uses_authorized_dataset_without_lazy_load(db_engine):
    with Session(db_engine) as session:
        group = AccessGroup(
            group_name="file-readers",
            group_type=AccessGroupType.CUSTOM.value,
        )
        dataset = Dataset(unique_id="file-dataset", dataset_name="Dataset")
        session.add_all([
            group,
            dataset,
            Resource(mfid="file-dataset", resource_type="dataset"),
        ])
        session.commit()
        session.refresh(group)
        session.refresh(dataset)
        session.add_all([
            ResourceACL(
                mfid="file-dataset",
                accessgroup_id=group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            AssociatedFile(
                mfid="file-mfid",
                filename="file.dat",
                size=1,
                sha256_hash="hash",
                dataset_id=dataset.id,
            ),
        ])
        session.commit()
        group_id = group.id
        group_name = group.group_name

    principal = Principal(
        user_id="reader",
        groups=(GroupMembership(
            id=group_id,
            group_name=group_name,
            role=Role.VIEWER,
        ),),
    )
    with _statement_count(db_engine) as statements:
        response = files_routes.get_file(principal, "file-mfid", MagicMock())

    assert len(statements) == 2
    assert response.dataset_mfid == "file-dataset"
