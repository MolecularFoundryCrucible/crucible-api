"""POST /service_accounts had no test coverage at all before this -- every
existing service-account test creates the User row directly and skips this
route entirely. Written while fixing a real bug: the SA's own personal
access group link was created with no role, which fails closed to viewer
(see _standing in auth_utils.py) instead of the owner standing a human user
gets at login (auth.py). Left uncaught, a service account recorded as a
resource's own owner_orcid would be capped at viewer on it via its own
group, unable to edit or delete something it's supposed to own.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, User, UserAccessGroupLink
from app.utils.permissions import PlatformRole


def test_creates_personal_group_at_owner_standing(client, db_engine, auth_as):
    auth_as("admin-1", platform_role=PlatformRole.ADMIN)

    resp = client.post("/service_accounts", json={"username": "ingest-bot"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "ingest-bot"
    assert body["is_service_account"] is True
    assert body["api_key"]
    sa_id = body["unique_id"]

    with Session(db_engine) as session:
        sa = session.exec(select(User).where(User.unique_id == sa_id)).one()
        assert sa.is_service_account is True
        assert sa.platform_role == PlatformRole.CONTRIBUTOR.value
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == sa_id)).one()
        link = session.exec(
            select(UserAccessGroupLink).where(UserAccessGroupLink.user_id == sa.id,
                                              UserAccessGroupLink.accessgroup_id == ag.id)
        ).one()
        assert link.role == "owner"


def test_requires_platform_admin(client, db_engine, auth_as):
    auth_as("0000-0001", platform_role=PlatformRole.NONE)

    resp = client.post("/service_accounts", json={"username": "sneaky-bot"})
    assert resp.status_code == 403


def test_username_must_be_unique_across_all_users(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(User(first_name="A", last_name="B", email="a@x.com",
                         unique_id="0000-0002", username="taken"))
        session.commit()

    auth_as("admin-2", platform_role=PlatformRole.ADMIN)
    resp = client.post("/service_accounts", json={"username": "taken"})
    assert resp.status_code == 409


def test_explicit_unique_id_is_honored_and_collision_rejected(client, db_engine, auth_as):
    auth_as("admin-3", platform_role=PlatformRole.ADMIN)
    fixed = "0tp5f0xedsa000000000000000"

    resp = client.post("/service_accounts", json={"username": "fixed-id-bot", "unique_id": fixed})
    assert resp.status_code == 200, resp.text
    assert resp.json()["unique_id"] == fixed

    resp = client.post("/service_accounts", json={"username": "another-bot", "unique_id": fixed})
    assert resp.status_code == 409


def test_explicit_unique_id_must_be_mfid(client, db_engine, auth_as):
    auth_as("admin-3", platform_role=PlatformRole.ADMIN)

    resp = client.post("/service_accounts", json={"username": "bad-id-bot", "unique_id": "sa-fixed-1"})
    assert resp.status_code == 422


def test_explicit_unique_id_must_not_collide_with_a_resource_or_group(client, db_engine, auth_as):
    from app.models.core_models import AccessGroup, Resource

    resource_mfid = "0tp5res0000000000000000001"
    group_mfid = "0tp5gr0000000000000000001a"
    with Session(db_engine) as session:
        session.add(Resource(mfid=resource_mfid, resource_type="dataset"))
        session.add(AccessGroup(group_name=group_mfid, group_type="project"))
        session.commit()
    auth_as("admin-3", platform_role=PlatformRole.ADMIN)

    for taken in (resource_mfid, group_mfid):
        resp = client.post("/service_accounts", json={"username": f"bot-{taken[-4:]}", "unique_id": taken})
        assert resp.status_code == 409, resp.text


def test_rotate_key_issues_a_new_key(client, db_engine, auth_as):
    auth_as("admin-4", platform_role=PlatformRole.ADMIN)
    create_resp = client.post("/service_accounts", json={"username": "rotate-bot"})
    sa_id = create_resp.json()["unique_id"]
    old_key = create_resp.json()["api_key"]

    resp = client.post(f"/service_accounts/{sa_id}/rotate_key")
    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key"] != old_key
