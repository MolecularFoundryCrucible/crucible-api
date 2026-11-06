from datetime import timedelta

from sqlmodel import Session, select

import tests.test_auth_linking as linking_tests
import tests.test_auth_providers as provider_tests
from app.cli import identity as identity_cli
from app.models.core_models import AuthEvent, Dataset, HubTokenFamily, User, UserIdentity
from tests.test_auth_linking import ADA, BOB, BOB_ORCID, LAB_ISSUER, ORCID_ISSUER, bearer, hub_session, user_id

remote = provider_tests.remote
world = linking_tests.world
ADMIN = "0tmnyhfztdwc1000w2kspzsnh9"
NEWCOMER = "0tmnyhfztdwc1000w2kspzsnh8"
ROBOT = "0tmnyhfztdwc1000w2kspzsnh7"


def admin_world(world, *, newcomer_orcid=None):
    _, client, engine = world
    with Session(engine) as session:
        session.add(User(unique_id=ADMIN, username="admin", first_name="A", last_name="D", email=None, platform_role="admin"))
        newcomer = User(unique_id=NEWCOMER, username="newcomer", first_name="N", last_name="C", email=None)
        session.add_all([newcomer, User(unique_id=ROBOT, username="robot", first_name="", last_name="", email=None, is_service_account=True)])
        session.flush()
        session.add(UserIdentity(user_id=newcomer.id, issuer=LAB_ISSUER, subject="newcomer-lab"))
        session.commit()
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    return client, engine, hub_session(engine, ADMIN)


def logins(engine, unique_id):
    with Session(engine) as session:
        return sorted((row.issuer, row.subject, row.verified_at is not None) for row in session.exec(select(UserIdentity).where(UserIdentity.user_id == user_id(engine, unique_id))).all())


def test_admin_adds_an_unverified_orcid_and_records_who(world):
    client, engine, admin = admin_world(world)
    response = client.post(f"/users/{NEWCOMER}/identities", headers=bearer(admin), json={"provider_key": "orcid", "subject": "0000-0004-0000-0004"})
    assert response.status_code == 201, response.text
    assert (response.json()["orcid"], response.json()["verified_at"]) == ("0000-0004-0000-0004", None)
    assert logins(engine, NEWCOMER) == [(LAB_ISSUER, "newcomer-lab", False), (ORCID_ISSUER, "0000-0004-0000-0004", False)]
    with Session(engine) as session:
        event = session.exec(select(AuthEvent)).one()
        assert (event.event, event.actor_user_id) == ("identity_linked", user_id(engine, ADMIN))
    assert [item["orcid"] for item in client.get(f"/users/{NEWCOMER}/identities", headers=bearer(admin)).json()] == [None, "0000-0004-0000-0004"]


def test_admin_cannot_add_a_login_someone_else_has_or_an_unlisted_issuer(world):
    client, _, admin = admin_world(world)
    taken = client.post(f"/users/{NEWCOMER}/identities", headers=bearer(admin), json={"provider_key": "orcid", "subject": BOB_ORCID})
    assert (taken.status_code, taken.json()["detail"]["code"]) == (409, "linked_elsewhere")
    assert client.post(f"/users/{NEWCOMER}/identities", headers=bearer(admin), json={"provider_key": "attacker", "subject": "x"}).status_code == 400
    robot = client.post(f"/users/{ROBOT}/identities", headers=bearer(admin), json={"provider_key": "lab", "subject": "robot-lab"})
    assert (robot.status_code, robot.json()["detail"]["code"]) == (409, "service_account")


def test_admin_identity_routes_need_a_recent_hub_session(world, auth_as):
    from app.utils.permissions import PlatformRole

    client, engine, _ = admin_world(world)
    stale = hub_session(engine, ADMIN, age=timedelta(hours=1))
    body = {"provider_key": "lab", "subject": "attacker-lab"}
    assert client.post(f"/users/{ADA}/identities", headers=bearer(stale), json=body).status_code == 401
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert client.post(f"/users/{ADA}/identities", json=body).json()["detail"]["code"] == "hub_session_required"
    assert client.delete(f"/users/{ADA}/identities/1").json()["detail"]["code"] == "hub_session_required"
    assert logins(engine, ADA) == [(ORCID_ISSUER, "0000-0002-1825-0097", True)]


def test_admin_removes_a_login_but_never_the_last_and_ends_their_sessions(world):
    client, engine, admin = admin_world(world)
    hub_session(engine, NEWCOMER)
    only = client.get(f"/users/{NEWCOMER}/identities", headers=bearer(admin)).json()[0]["id"]
    assert client.delete(f"/users/{NEWCOMER}/identities/{only}", headers=bearer(admin)).json()["detail"]["code"] == "last_sign_in_method"
    added = client.post(f"/users/{NEWCOMER}/identities", headers=bearer(admin), json={"provider_key": "orcid", "subject": "0000-0004-0000-0004"}).json()["id"]
    assert client.delete(f"/users/{NEWCOMER}/identities/{only}", headers=bearer(admin)).status_code == 204
    assert logins(engine, NEWCOMER) == [(ORCID_ISSUER, "0000-0004-0000-0004", False)]
    assert client.delete(f"/users/{BOB}/identities/{added}", headers=bearer(admin)).status_code == 404
    with Session(engine) as session:
        families = session.exec(select(HubTokenFamily).where(HubTokenFamily.user_id == user_id(engine, NEWCOMER))).all()
        assert families and all(family.revoked_at is not None for family in families)


def move(subject, target, issuer=LAB_ISSUER):
    return identity_cli._run_move(type("Args", (), {"issuer": issuer, "subject": subject, "target": target, "yes": True})())


def test_move_takes_a_login_off_an_empty_account_and_switches_it_off(world, use_engine, capsys):
    client, engine, _ = admin_world(world)
    use_engine(engine)
    hub_session(engine, NEWCOMER)
    assert move("newcomer-lab", ADA) == 0
    assert logins(engine, ADA) == [(LAB_ISSUER, "newcomer-lab", False), (ORCID_ISSUER, "0000-0002-1825-0097", True)]
    assert logins(engine, NEWCOMER) == []
    with Session(engine) as session:
        newcomer = session.exec(select(User).where(User.unique_id == NEWCOMER)).one()
        assert newcomer.disabled_at is not None
        assert all(family.revoked_at is not None for family in session.exec(select(HubTokenFamily).where(HubTokenFamily.user_id == newcomer.id)).all())
        assert session.exec(select(AuthEvent.event)).all() == ["identity_unlinked", "identity_linked", "disabled"]


def test_move_refuses_an_account_that_holds_anything(world, use_engine, capsys):
    client, engine, _ = admin_world(world)
    use_engine(engine)
    with Session(engine) as session:
        session.add(Dataset(unique_id="ds-newcomer", dataset_name="d", owner_orcid=NEWCOMER, owner_user_id=user_id(engine, NEWCOMER)))
        session.commit()
    assert move("newcomer-lab", ADA) == 1
    assert "holds 1 datasets" in capsys.readouterr().err
    assert logins(engine, NEWCOMER) == [(LAB_ISSUER, "newcomer-lab", False)]


def test_move_refuses_a_second_orcid_and_unknown_people(world, use_engine, capsys):
    client, engine, admin = admin_world(world)
    use_engine(engine)
    with Session(engine) as session:
        session.add(UserIdentity(user_id=user_id(engine, NEWCOMER), issuer=ORCID_ISSUER, subject="0000-0004-0000-0004"))
        session.commit()
    assert move("0000-0004-0000-0004", BOB, issuer=ORCID_ISSUER) == 1
    assert "already has an ORCID" in capsys.readouterr().err
    assert move("0000-0004-0000-0004", ADA, issuer=ORCID_ISSUER) == 1
    assert move("no-such-login", ADA) == 1
    assert move("newcomer-lab", "nobody") == 1


def test_people_see_their_own_logins_but_not_others(world, auth_as):
    _, client, engine = world
    admin_world(world)
    client.headers.pop("Crucible-Privilege-Mode", None)
    auth_as(ADA)
    own = client.get(f"/users/{ADA}/identities")
    assert own.status_code == 200, own.text
    assert [item["orcid"] for item in own.json()] == ["0000-0002-1825-0097"]
    assert [item["orcid"] for item in client.get("/account/identities").json()] == ["0000-0002-1825-0097"]
    assert client.get(f"/users/{BOB}/identities").status_code == 403

