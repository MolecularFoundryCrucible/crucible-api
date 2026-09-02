from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.models.core_models import Project
from app.utils.project_utils import resolve_project_id_to_mfid, resolve_project_mfids_to_ids, resolve_project_mfids_to_references


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[Project.__table__])
    return engine


def _project(session, project_id, lead_orcid):
    proj = Project(project_id=project_id, organization="LBNL", project_lead_orcid=lead_orcid)
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


def test_resolve_project_id_to_mfid():
    with Session(_engine()) as session:
        proj = _project(session, "proj_a", "0000-0001")
        assert resolve_project_id_to_mfid("proj_a", session) == proj.unique_id


def test_resolve_unknown_project_id_returns_none():
    with Session(_engine()) as session:
        assert resolve_project_id_to_mfid("no-such-project", session) is None


def test_resolve_project_mfids_to_ids_is_batched():
    with Session(_engine()) as session:
        p1 = _project(session, "proj_a", "0000-0001")
        p2 = _project(session, "proj_b", "0000-0002")
        result = resolve_project_mfids_to_ids([p1.unique_id, p2.unique_id, "no-such-mfid"], session)
        assert result == {p1.unique_id: "proj_a", p2.unique_id: "proj_b"}


def test_resolve_project_mfids_to_ids_empty_input():
    with Session(_engine()) as session:
        assert resolve_project_mfids_to_ids([], session) == {}
        assert resolve_project_mfids_to_ids([None, None], session) == {}


def test_resolve_project_mfids_to_references():
    with Session(_engine()) as session:
        project = _project(session, "proj_a", "0000-0001")
        project.title = "Project A"
        session.add(project)
        session.commit()
        references = resolve_project_mfids_to_references([project.unique_id, "missing"], session)
        assert references[project.unique_id].model_dump() == {
            "unique_id": project.unique_id,
            "project_id": "proj_a",
            "title": "Project A",
        }
