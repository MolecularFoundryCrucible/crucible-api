"""POST /projects accepts project_lead (ORCID, username, or email, resolved
via resolve_user_by_identifier -- the same flexible-field pattern as
transfer_ownership's new_owner and dataset/sample's owner) alongside the
existing three explicit project_lead_orcid/_email/_username fields.
"""
from sqlmodel import Session, select

from app.tables import Project, User
from app.utils.permissions import PlatformRole


def _user(session, orcid, username=None):
    u = User(first_name="A", last_name="Lead", email=f"{orcid}@x.com",
            unique_id=orcid, username=username)
    session.add(u)
    session.commit()
    return u


def test_project_lead_by_orcid_resolves(client, db_engine, auth_as):
    with Session(db_engine) as session:
        _user(session, "0000-0001-1111-1111")
    auth_as("0000-0001-1111-1111", platform_role=PlatformRole.NONE)

    resp = client.post("/projects", json={
        "project_id": "proj_a", "organization": "LBNL", "project_lead": "0000-0001-1111-1111",
    })
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == "proj_a")).one()
        assert proj.project_lead_orcid == "0000-0001-1111-1111"


def test_project_lead_by_email_resolves(client, db_engine, auth_as):
    with Session(db_engine) as session:
        _user(session, "0000-0002-2222-2222")
    auth_as("0000-0002-2222-2222", platform_role=PlatformRole.NONE)

    resp = client.post("/projects", json={
        "project_id": "proj_b", "organization": "LBNL", "project_lead": "0000-0002-2222-2222@x.com",
    })
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == "proj_b")).one()
        assert proj.project_lead_orcid == "0000-0002-2222-2222"


def test_project_lead_by_username_resolves(client, db_engine, auth_as):
    with Session(db_engine) as session:
        _user(session, "0000-0003-3333-3333", username="projlead")
    auth_as("0000-0003-3333-3333", platform_role=PlatformRole.NONE)

    resp = client.post("/projects", json={
        "project_id": "proj_c", "organization": "LBNL", "project_lead": "projlead",
    })
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == "proj_c")).one()
        assert proj.project_lead_orcid == "0000-0003-3333-3333"


def test_project_lead_and_project_lead_orcid_together_is_rejected(client, db_engine, auth_as):
    with Session(db_engine) as session:
        _user(session, "0000-0004-4444-4444")
    auth_as("0000-0004-4444-4444", platform_role=PlatformRole.NONE)

    resp = client.post("/projects", json={
        "project_id": "proj_d", "organization": "LBNL",
        "project_lead": "0000-0004-4444-4444", "project_lead_orcid": "0000-0004-4444-4444",
    })
    assert resp.status_code == 400


def test_no_lead_field_is_rejected(client, db_engine, auth_as):
    auth_as("0000-0005-5555-5555", platform_role=PlatformRole.NONE)

    resp = client.post("/projects", json={"project_id": "proj_e", "organization": "LBNL"})
    assert resp.status_code == 400


def test_unresolvable_project_lead_is_404(client, db_engine, auth_as):
    auth_as("0000-0006-6666-6666", platform_role=PlatformRole.NONE)

    resp = client.post("/projects", json={
        "project_id": "proj_f", "organization": "LBNL", "project_lead": "nobody@x.com",
    })
    assert resp.status_code == 404


def test_legacy_project_lead_email_field_still_works(client, db_engine, auth_as):
    with Session(db_engine) as session:
        _user(session, "0000-0007-7777-7777")
    auth_as("0000-0007-7777-7777", platform_role=PlatformRole.NONE)

    resp = client.post("/projects", json={
        "project_id": "proj_g", "organization": "LBNL", "project_lead_email": "0000-0007-7777-7777@x.com",
    })
    assert resp.status_code == 200, resp.text
