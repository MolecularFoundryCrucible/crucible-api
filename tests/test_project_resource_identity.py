from fastapi import Response
from datetime import datetime

from sqlmodel import Session

from app.api.routes.projects import read_project
from app.models.core_models import Project, Resource, User
from app.utils.permissions import Principal


def test_project_response_loads_resource_by_project_mfid(db_engine):
    owner_id = "0000-0001-6402-3752"
    with Session(db_engine) as session:
        session.add(User(
            first_name="Project",
            last_name="Owner",
            email="owner@example.org",
            unique_id=owner_id,
        ))
        project = Project(
            project_id="resource-identity",
            organization="LBNL",
            project_lead_orcid=owner_id,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        project_mfid = project.unique_id
        session.add(Resource(
            mfid=project_mfid,
            resource_type="project",
            creation_time=datetime.fromisoformat("2026-08-31T12:00:00+00:00"),
            modification_time=datetime.fromisoformat("2026-08-31T13:00:00+00:00"),
        ))
        session.commit()

    result = read_project(
        project_ref=project_mfid,
        response=Response(),
        auth_res=Principal(user_id=owner_id),
    )

    assert result.creation_time == datetime.fromisoformat("2026-08-31T12:00:00+00:00")
    assert result.modification_time == datetime.fromisoformat("2026-08-31T13:00:00+00:00")
    serialized = result.model_dump(mode="json")
    assert serialized["creation_time"] == "2026-08-31T12:00:00.000000Z"
    assert serialized["modification_time"] == "2026-08-31T13:00:00.000000Z"
