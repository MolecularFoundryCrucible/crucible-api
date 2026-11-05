from unittest.mock import MagicMock

import pytest
import requests
from sqlmodel import Session, select

from app.auth.identity import linked_orcid
from app.config import get_settings
from app.models.core_models import AccessGroup, DeletionRequest, Project, User, UserAccessGroupLink, UserIdentity
from app.services import proposals
from app.services.proposals import ProposalServiceUnavailable, lookup_proposals

ORCID = "0000-0002-1825-0097"
MFID = "0tmnyhfztdwc1000w2kspzsnh0"
ISSUER = "https://orcid.org"
CALLER = ORCID


@pytest.fixture(autouse=True)
def service(monkeypatch):
    monkeypatch.setenv("PROPOSAL_SYNC_ENABLED", "true")
    monkeypatch.setenv("PROPOSAL_DB_URL", "https://proposals.example/api")
    monkeypatch.setenv("PROPDB_APIKEY", "key")
    get_settings.cache_clear()


@pytest.fixture
def lookup_on(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_IDENTITY_LOOKUP", "true")
    get_settings.cache_clear()


def answers(monkeypatch, *proposal_ids):
    seen = []

    def lookup(orcid):
        seen.append(orcid)
        return sorted(proposal_ids)

    monkeypatch.setattr("app.api.routes.account.lookup_proposals", lookup)
    return seen


def person(db_engine, unique_id=ORCID, linked=None, **fields):
    with Session(db_engine) as session, session.begin():
        user = User(**{"unique_id": unique_id, "first_name": "Ada", "last_name": "Lovelace", "email": None, **fields})
        session.add(user)
        session.flush()
        if linked:
            session.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=linked))


def project(db_engine, project_id="MF-1", group=True, pending=False):
    unique_id = project_id.lower().replace("-", "x").ljust(26, "0")
    with Session(db_engine) as session, session.begin():
        session.add(Project(project_id=project_id, unique_id=unique_id, organization="MF", project_lead_orcid="lead"))
        if group:
            session.add(AccessGroup(group_name=unique_id, group_type="project"))
        if pending:
            session.add(DeletionRequest(resource_id=unique_id, resource_type="project", requester_id=ORCID, status="pending"))
    return unique_id


def memberships(db_engine, unique_id=ORCID):
    with Session(db_engine) as session:
        return session.exec(
            select(AccessGroup.group_name).join(UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id).join(User, User.id == UserAccessGroupLink.user_id).where(User.unique_id == unique_id)
        ).all()


def sync(client, auth_as, caller=CALLER):
    auth_as(caller)
    return client.post("/account/sync-projects")


def test_sync_adds_projects_from_the_linked_orcid(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, MFID, linked=ORCID)
    group = project(db_engine)
    seen = answers(monkeypatch, "MF-1")
    response = sync(client, auth_as, MFID)
    assert response.status_code == 200, response.text
    assert seen == [ORCID]
    assert response.json() == {"orcid": ORCID, "projects_added": ["MF-1"], "projects_already_member": [], "projects_not_found": []}
    assert group in memberships(db_engine, MFID)


def test_sync_works_for_an_unverified_orcid(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, ORCID, linked=ORCID)
    project(db_engine)
    answers(monkeypatch, "MF-1")
    with Session(db_engine) as session:
        assert session.exec(select(UserIdentity)).one().verified_at is None
    assert sync(client, auth_as).json()["projects_added"] == ["MF-1"]


def test_second_sync_reports_already_member(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, ORCID, linked=ORCID)
    project(db_engine)
    answers(monkeypatch, "MF-1")
    sync(client, auth_as)
    assert sync(client, auth_as).json()["projects_already_member"] == ["MF-1"]


def test_sync_finds_nothing_after_unlink(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, ORCID)
    project(db_engine)
    seen = answers(monkeypatch, "MF-1")
    response = sync(client, auth_as)
    assert seen == []
    assert response.json() == {"orcid": None, "projects_added": [], "projects_already_member": [], "projects_not_found": []}


def test_sync_before_the_backfill_still_uses_an_orcid_shaped_id(client, auth_as, db_engine, monkeypatch):
    person(db_engine, ORCID)
    project(db_engine)
    seen = answers(monkeypatch, "MF-1")
    assert sync(client, auth_as).json()["orcid"] == ORCID
    assert seen == [ORCID]


def test_sync_ignores_an_orcid_shaped_service_account_id(db_engine):
    person(db_engine, ORCID, is_service_account=True)
    with Session(db_engine) as session:
        assert linked_orcid(session, session.exec(select(User)).one()) is None


def test_person_with_no_orcid_gets_an_empty_result(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, MFID)
    seen = answers(monkeypatch, "MF-1")
    assert sync(client, auth_as, MFID).json()["projects_added"] == []
    assert seen == []


def test_sync_returns_502_when_the_proposal_service_is_down(client, auth_as, db_engine, monkeypatch):
    person(db_engine, ORCID)
    monkeypatch.setattr(proposals.requests, "get", MagicMock(side_effect=requests.ConnectionError("down")))
    response = sync(client, auth_as)
    assert response.status_code == 502
    assert "key" not in response.text.replace("detail", "")


def test_sync_skips_projects_pending_deletion(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, ORCID, linked=ORCID)
    group = project(db_engine, pending=True)
    answers(monkeypatch, "MF-1")
    result = sync(client, auth_as).json()
    assert result["projects_added"] == [] and result["projects_not_found"] == ["MF-1"]
    assert group not in memberships(db_engine)


def test_sync_reports_unknown_projects(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, ORCID, linked=ORCID)
    answers(monkeypatch, "MF-404")
    assert sync(client, auth_as).json()["projects_not_found"] == ["MF-404"]


def test_sync_does_not_invent_a_missing_access_group(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, ORCID, linked=ORCID)
    project(db_engine, group=False)
    answers(monkeypatch, "MF-1")
    assert sync(client, auth_as).json()["projects_not_found"] == ["MF-1"]
    with Session(db_engine) as session:
        assert session.exec(select(AccessGroup)).all() == []


def test_sync_never_removes_a_membership(client, auth_as, db_engine, monkeypatch, lookup_on):
    person(db_engine, ORCID, linked=ORCID)
    group = project(db_engine)
    answers(monkeypatch, "MF-1")
    sync(client, auth_as)
    answers(monkeypatch)
    sync(client, auth_as)
    assert group in memberships(db_engine)


@pytest.mark.parametrize("payload", [{"not": "a list"}, ["not a dict"], [{"proposals": "not a list"}]])
def test_invalid_answer_counts_as_unavailable(monkeypatch, payload):
    monkeypatch.setattr(proposals.requests, "get", MagicMock(return_value=MagicMock(json=MagicMock(return_value=payload))))
    with pytest.raises(ProposalServiceUnavailable):
        lookup_proposals(ORCID)


def test_creating_a_person_carries_on_when_the_service_is_down(monkeypatch):
    monkeypatch.setattr(proposals.requests, "get", MagicMock(side_effect=requests.ConnectionError("down")))
    assert proposals.find_mf_user_proposals(ORCID) == []
