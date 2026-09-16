import pytest
from fastapi import HTTPException, Request
from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup,
    Dataset,
    Project,
    Resource,
    ResourceACL,
    Sample,
    User,
    UserAccessGroupLink,
)
from app.api.routes.projects import list_projects
from app.api.routes.datasets import list_datasets, search_datasets
from app.api.routes.samples import list_samples, search_samples
from app.models.query_params import (
    DatasetListQuery, ProjectListQuery, ResourceSearchQuery, SampleListQuery)
from app.utils.auth_utils import access_selector_filter, project_scope_filter
from app.utils.permissions import AccessGroupType, GroupMembership, PlatformRole, Principal, Role
from app.utils.project_utils import resolve_project_filter_mfid


def _seed(db_engine):
    with Session(db_engine) as session:
        alice = User(first_name="Alice", last_name="User", email="alice@example.com", username="alice", unique_id="0000-0001-1111-1111")
        bob = User(first_name="Bob", last_name="User", email="bob@example.com", username="bob-user", unique_id="0000-0002-2222-2222")
        session.add_all([alice, bob])
        session.commit()
        session.refresh(alice)
        session.refresh(bob)

        alice_group = AccessGroup(group_name=alice.unique_id, group_type=AccessGroupType.IDENTITY.value)
        bob_group = AccessGroup(group_name=bob.unique_id, group_type=AccessGroupType.IDENTITY.value)
        project_x = Project(project_id="project-x", organization="LBNL", project_lead_orcid=alice.unique_id)
        project_y = Project(project_id="project-y", organization="LBNL", project_lead_orcid=alice.unique_id)
        session.add_all([alice_group, bob_group, project_x, project_y])
        session.commit()
        session.refresh(alice_group)
        session.refresh(bob_group)
        session.refresh(project_x)
        session.refresh(project_y)

        project_x_group = AccessGroup(group_name=project_x.unique_id, group_type=AccessGroupType.PROJECT.value)
        project_y_group = AccessGroup(group_name=project_y.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add_all([project_x_group, project_y_group])
        session.commit()
        session.refresh(project_x_group)
        session.refresh(project_y_group)

        session.add_all([
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=alice_group.id, role=Role.OWNER.db_value),
            UserAccessGroupLink(user_id=bob.id, accessgroup_id=bob_group.id, role=Role.OWNER.db_value),
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=project_x_group.id, role=Role.VIEWER.db_value),
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=project_y_group.id, role=Role.VIEWER.db_value),
        ])
        session.add_all([
            Resource(mfid=project_x.unique_id, resource_type="project"),
            Resource(mfid=project_y.unique_id, resource_type="project"),
            ResourceACL(mfid=project_x.unique_id, accessgroup_id=project_x_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid=project_y.unique_id, accessgroup_id=project_y_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid=project_y.unique_id, accessgroup_id=project_x_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
        ])

        both_users = Dataset(unique_id="ds-both-users", dataset_name="both users")
        both_projects = Dataset(
            unique_id="ds-both-projects",
            dataset_name="both projects",
            project_mfid=project_y.unique_id,
        )
        assigned_project = Dataset(
            unique_id="ds-assigned-project",
            dataset_name="assigned project",
            project_mfid=project_x.unique_id,
        )
        alice_only = Dataset(unique_id="ds-alice-only", dataset_name="alice only")
        assigned_sample = Sample(
            unique_id="sample-assigned-project",
            sample_name="assigned project",
            project_mfid=project_x.unique_id,
        )
        shared_sample = Sample(
            unique_id="sample-shared-project",
            sample_name="shared project",
            project_mfid=project_y.unique_id,
        )
        session.add_all([
            both_users,
            both_projects,
            assigned_project,
            alice_only,
            assigned_sample,
            shared_sample,
        ])
        session.add_all([
            Resource(mfid=both_users.unique_id, resource_type="dataset"),
            Resource(mfid=both_projects.unique_id, resource_type="dataset"),
            Resource(mfid=assigned_project.unique_id, resource_type="dataset"),
            Resource(mfid=alice_only.unique_id, resource_type="dataset"),
            Resource(mfid=assigned_sample.unique_id, resource_type="sample"),
            Resource(mfid=shared_sample.unique_id, resource_type="sample"),
        ])
        session.add_all([
            ResourceACL(mfid=both_users.unique_id, accessgroup_id=alice_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=both_users.unique_id, accessgroup_id=bob_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=alice_only.unique_id, accessgroup_id=alice_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=both_projects.unique_id, accessgroup_id=project_x_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=both_projects.unique_id, accessgroup_id=project_y_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=assigned_project.unique_id, accessgroup_id=project_x_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=assigned_sample.unique_id, accessgroup_id=project_x_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=shared_sample.unique_id, accessgroup_id=project_x_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
        ])
        session.commit()
        return {
            "project_x_id": project_x.project_id,
            "project_x_mfid": project_x.unique_id,
            "project_x_group_id": project_x_group.id,
            "project_y_mfid": project_y.unique_id,
            "project_y_group_id": project_y_group.id,
        }


def test_user_selectors_intersect_effective_access(db_engine):
    _seed(db_engine)
    caller = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    with Session(db_engine) as session:
        clause = access_selector_filter(
            session,
            caller,
            Dataset,
            user_refs=["alice@example.com", "bob-user"],
        )
        rows = session.exec(select(Dataset.unique_id).where(clause)).all()

    assert rows == ["ds-both-users"]


def test_platform_contributor_can_select_another_user(db_engine):
    _seed(db_engine)
    caller = Principal(
        user_id="uploader",
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    with Session(db_engine) as session:
        clause = access_selector_filter(
            session,
            caller,
            Dataset,
            user_refs=["alice@example.com"],
        )
        rows = session.exec(select(Dataset.unique_id).where(clause)).all()

    assert sorted(rows) == [
        "ds-alice-only",
        "ds-assigned-project",
        "ds-both-projects",
        "ds-both-users",
    ]


def test_service_account_type_alone_cannot_select_another_user(db_engine):
    _seed(db_engine)
    caller = Principal(user_id="uploader", is_service_account=True)

    with Session(db_engine) as session:
        with pytest.raises(HTTPException) as unauthorized:
            access_selector_filter(
                session,
                caller,
                Dataset,
                user_refs=["alice@example.com"],
            )

    assert unauthorized.value.status_code == 403


def test_project_selectors_accept_slug_and_mfid_and_intersect(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(id=seeded["project_x_group_id"], group_name=seeded["project_x_mfid"], role=Role.VIEWER),
            GroupMembership(id=seeded["project_y_group_id"], group_name=seeded["project_y_mfid"], role=Role.VIEWER),
        ),
    )

    with Session(db_engine) as session:
        clause = access_selector_filter(
            session,
            caller,
            Dataset,
            project_refs=["project-x", seeded["project_y_mfid"]],
        )
        rows = session.exec(select(Dataset.unique_id).where(clause)).all()

    assert rows == ["ds-both-projects"]


def test_project_selector_requires_membership_and_limits_selector_count(db_engine):
    _seed(db_engine)
    caller = Principal(user_id="outsider")

    with Session(db_engine) as session:
        with pytest.raises(HTTPException) as unauthorized:
            access_selector_filter(session, caller, Dataset, project_refs=["project-x"])
        with pytest.raises(HTTPException) as excessive:
            access_selector_filter(session, caller, Dataset, user_refs=["alice"] * 11)

    assert unauthorized.value.status_code == 403
    assert excessive.value.status_code == 422


@pytest.mark.parametrize(
    ("resource_model", "scope", "expected"),
    [
        (Dataset, "assigned", ["ds-assigned-project"]),
        (Dataset, "shared", ["ds-both-projects"]),
        (Dataset, "all", ["ds-assigned-project", "ds-both-projects"]),
        (Sample, "assigned", ["sample-assigned-project"]),
        (Sample, "shared", ["sample-shared-project"]),
        (Sample, "all", ["sample-assigned-project", "sample-shared-project"]),
    ],
)
def test_project_scope_filter_distinguishes_assignment_and_sharing(
    db_engine, resource_model, scope, expected
):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(
                id=seeded["project_x_group_id"],
                group_name=seeded["project_x_mfid"],
                role=Role.VIEWER,
            ),
        ),
    )

    with Session(db_engine) as session:
        clause = project_scope_filter(
            session,
            caller,
            resource_model,
            seeded["project_x_mfid"],
            scope,
        )
        rows = session.exec(
            select(resource_model.unique_id).where(clause).order_by(resource_model.unique_id)
        ).all()

    assert rows == expected


@pytest.mark.parametrize("route", [list_datasets, list_samples])
def test_project_scope_requires_project_identifier(route):
    request = Request({"type": "http", "query_string": b"project_scope=all", "headers": []})
    query_type = DatasetListQuery if route is list_datasets else SampleListQuery

    with pytest.raises(ValueError, match="project_scope requires project_id or project_mfid"):
        route(
            auth_res=Principal(user_id="admin", platform_role=PlatformRole.ADMIN),
            request=request,
            params=query_type(project_scope="all"),
        )


@pytest.mark.parametrize("route", [search_datasets, search_samples])
def test_project_scoped_search_requires_project_identifier(route):
    with pytest.raises(ValueError, match="project_scope requires project_id or project_mfid"):
        route(
            auth_res=Principal(user_id="admin", platform_role=PlatformRole.ADMIN),
            params=ResourceSearchQuery(q="search term", project_scope="all"),
        )


def test_project_filter_accepts_matching_slug_and_mfid(db_engine):
    seeded = _seed(db_engine)

    with Session(db_engine) as session:
        resolved = resolve_project_filter_mfid(
            session,
            seeded["project_x_id"],
            seeded["project_x_mfid"],
        )

    assert resolved == seeded["project_x_mfid"]


def test_project_filter_rejects_conflicting_slug_and_mfid(db_engine):
    seeded = _seed(db_engine)

    with Session(db_engine) as session:
        with pytest.raises(HTTPException) as error:
            resolve_project_filter_mfid(
                session,
                seeded["project_x_id"],
                seeded["project_y_mfid"],
            )

    assert error.value.status_code == 422
    assert error.value.detail == "project_id and project_mfid identify different projects"


def test_project_filter_rejects_unknown_slug(db_engine):
    _seed(db_engine)

    with Session(db_engine) as session:
        with pytest.raises(HTTPException) as error:
            resolve_project_filter_mfid(session, "missing-project", None)

    assert error.value.status_code == 404


def test_project_scope_collection_responses_report_relation(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(
                id=seeded["project_x_group_id"],
                group_name=seeded["project_x_mfid"],
                role=Role.VIEWER,
            ),
        ),
    )
    query_string = f"project_id={seeded['project_x_id']}&project_scope=all".encode()

    datasets = list_datasets(
        auth_res=caller,
        request=Request({"type": "http", "query_string": query_string, "headers": []}),
        params=DatasetListQuery(
            project_id=seeded["project_x_id"],
            project_scope="all",
        ),
    )
    samples = list_samples(
        auth_res=caller,
        request=Request({"type": "http", "query_string": query_string, "headers": []}),
        params=SampleListQuery(
            project_id=seeded["project_x_id"],
            project_scope="all",
        ),
    )

    assert {
        item.unique_id: item.project_relation for item in datasets.items
    } == {
        "ds-assigned-project": "assigned",
        "ds-both-projects": "shared",
    }
    assert {
        item.unique_id: item.project_relation for item in samples.items
    } == {
        "sample-assigned-project": "assigned",
        "sample-shared-project": "shared",
    }


def test_dataset_cursor_precedence_and_total_control(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(id=seeded["project_x_group_id"], group_name=seeded["project_x_mfid"], role=Role.VIEWER),
            GroupMembership(id=seeded["project_y_group_id"], group_name=seeded["project_y_mfid"], role=Role.VIEWER),
        ),
    )
    request = Request({"type": "http", "query_string": b"", "headers": []})

    first = list_datasets(
        auth_res=caller,
        request=request,
        params=DatasetListQuery(limit=1),
    )
    assert first.total == 2
    assert first.next_cursor is not None

    continuation = list_datasets(
        auth_res=caller,
        request=request,
        params=DatasetListQuery(
            limit=1,
            cursor=first.next_cursor,
            offset=1000,
            include_total=True,
        ),
    )
    assert continuation.total == 2
    assert continuation.offset is None
    assert len(continuation.items) == 1

    suppressed = list_datasets(
        auth_res=caller,
        request=request,
        params=DatasetListQuery(include_total=False),
    )
    assert suppressed.total is None


def test_project_collection_selector_includes_owned_and_shared_projects(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(id=seeded["project_x_group_id"], group_name=seeded["project_x_mfid"], role=Role.VIEWER),
            GroupMembership(id=seeded["project_y_group_id"], group_name=seeded["project_y_mfid"], role=Role.VIEWER),
        ),
    )

    response = list_projects(
        auth_res=caller,
        params=ProjectListQuery(accessible_to_project=["project-x"]),
    )

    assert {project.project_id for project in response.items} == {"project-x", "project-y"}
    assert all(not hasattr(project.lead, "email") for project in response.items)


def test_exact_project_slug_collection_can_include_members(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(id=seeded["project_x_group_id"], group_name=seeded["project_x_mfid"], role=Role.VIEWER),
        ),
    )

    response = list_projects(
        auth_res=caller,
        params=ProjectListQuery(project_id="project-x", include_members=True),
    )

    assert response.total == 1
    assert [member.unique_id for member in response.items[0].members] == ["0000-0001-1111-1111"]


def test_project_collection_member_expansion_requires_exact_slug():
    caller = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    with pytest.raises(ValueError, match="include_members requires an exact project_id filter"):
        list_projects(
            auth_res=caller,
            params=ProjectListQuery(include_members=True),
        )


def test_access_selectors_are_repeated_openapi_arrays():
    from app.api.main import app

    schema = app.openapi()
    assert schema["components"]["schemas"]["InstrumentStatus"]["enum"] == [
        "active",
        "maintenance",
        "decommissioned",
    ]
    for path in ("/datasets", "/samples", "/projects"):
        parameters = {
            parameter["name"]: parameter
            for parameter in schema["paths"][path]["get"]["parameters"]
        }
        assert parameters["accessible_to_user"]["schema"]["anyOf"][0]["type"] == "array"
        assert parameters["accessible_to_project"]["schema"]["anyOf"][0]["type"] == "array"
        if path == "/projects":
            assert parameters["include_members"]["schema"]["default"] is False
        assert parameters["include_total"]["required"] is False
        assert "accessible_to" not in parameters
    dataset_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/datasets"]["get"]["parameters"]
    }
    assert dataset_parameters["instrument_mfid"]["required"] is False
    assert dataset_parameters["instrument_mfid"]["description"] == (
        "Filter datasets assigned to this canonical instrument MFID."
    )
    assert dataset_parameters["sample_mfid"]["required"] is False
    assert dataset_parameters["sample_mfid"]["description"] == (
        "Filter datasets linked to this canonical sample MFID."
    )
    sample_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/samples"]["get"]["parameters"]
    }
    assert sample_parameters["dataset_mfid"]["required"] is False
    assert sample_parameters["dataset_mfid"]["description"] == (
        "Filter samples linked to this canonical dataset MFID."
    )
    for parameters in (dataset_parameters, sample_parameters):
        assert parameters["owner_id"]["required"] is False
        assert parameters["owner_id"]["schema"]["anyOf"][0]["minLength"] == 1
        assert parameters["project_id"]["required"] is False
        assert parameters["project_mfid"]["required"] is False
        assert parameters["project_scope"]["required"] is False
        assert parameters["project_scope"]["schema"]["anyOf"][0]["enum"] == [
            "assigned",
            "shared",
            "all",
        ]
    for path in ("/datasets/search", "/samples/search"):
        parameters = {
            parameter["name"]: parameter
            for parameter in schema["paths"][path]["get"]["parameters"]
        }
        assert parameters["q"]["schema"]["maxLength"] == 200
        assert parameters["owner_id"]["required"] is False
        assert parameters["project_id"]["required"] is False
        assert parameters["project_mfid"]["required"] is False
        assert parameters["project_scope"]["schema"]["anyOf"][0]["enum"] == [
            "assigned",
            "shared",
            "all",
        ]
    instrument_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/instruments"]["get"]["parameters"]
    }
    assert instrument_parameters["owner_id"]["required"] is False
    assert instrument_parameters["include_total"]["required"] is False
    instrument_search_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/instruments/search"]["get"]["parameters"]
    }
    assert instrument_search_parameters["owner_id"]["required"] is False
    assert schema["paths"]["/samples/{sample_id}/datasets"]["get"]["deprecated"] is True
    assert schema["paths"]["/datasets/{dsid}/samples"]["get"]["deprecated"] is True
    for schema_name in ("DatasetResponse", "SampleResponse"):
        capabilities = schema["components"]["schemas"][schema_name]["properties"]["capabilities"]
        assert capabilities["anyOf"][0]["$ref"] == "#/components/schemas/ResourceCapabilities"
    dataset_properties = schema["components"]["schemas"]["DatasetResponse"]["properties"]
    assert dataset_properties["instrument"]["anyOf"][0]["$ref"] == "#/components/schemas/InstrumentReferenceRead"
    assert dataset_properties["project"]["anyOf"][0]["$ref"] == "#/components/schemas/ProjectReferenceRead"
    assert dataset_properties["owner"]["anyOf"][0]["$ref"] == "#/components/schemas/UserPublicRead"
    assert dataset_properties["project_relation"]["anyOf"][0]["enum"] == ["assigned", "shared"]
    for schema_name in ("SampleRead", "SampleResponse"):
        sample_properties = schema["components"]["schemas"][schema_name]["properties"]
        assert sample_properties["project"]["anyOf"][0]["$ref"] == "#/components/schemas/ProjectReferenceRead"
    sample_response_properties = schema["components"]["schemas"]["SampleResponse"]["properties"]
    assert sample_response_properties["owner"]["anyOf"][0]["$ref"] == "#/components/schemas/UserPublicRead"
    assert sample_response_properties["project_relation"]["anyOf"][0]["enum"] == ["assigned", "shared"]
    assert sample_response_properties["datasets"]["deprecated"] is True
    for path in (
        "/datasets/{dataset_mfid}",
        "/samples/{sample_mfid}",
        "/instruments/{instrument_mfid}",
        "/resources/{resource_id}",
    ):
        parameters = {
            parameter["name"]: parameter
            for parameter in schema["paths"][path]["get"]["parameters"]
        }
        assert parameters["include_owner"]["schema"]["default"] is True
    for path in ("/samples/{sample_mfid}", "/resources/{resource_id}"):
        parameters = {
            parameter["name"]: parameter
            for parameter in schema["paths"][path]["get"]["parameters"]
        }
        assert parameters["include_datasets"]["schema"]["default"] is True
