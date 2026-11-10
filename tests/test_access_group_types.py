import importlib.util
from pathlib import Path

from sqlmodel import Session, select

from app.tables import AccessGroup, Instrument, Project, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PlatformRole, Role


def _load_backfill_module():
    path = Path(__file__).parents[1] / "migrations/data/20260827_backfill_access_group_types.py"
    spec = importlib.util.spec_from_file_location("backfill_access_group_types", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_data_script(filename):
    path = Path(__file__).parents[1] / "migrations/data" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classifier_uses_entity_ownership_not_identifier_format():
    module = _load_backfill_module()
    user_ids = {"0000-0001", "0sa00000000000000000000000"}
    project_ids = {"0project000000000000000000"}
    instrument_ids = {"0instrument000000000000000"}

    assert module.classify_group_name("0000-0001", user_ids, project_ids, instrument_ids)[0] == "identity"
    assert module.classify_group_name("0sa00000000000000000000000", user_ids, project_ids, instrument_ids)[0] == "identity"
    assert module.classify_group_name("0project000000000000000000", user_ids, project_ids, instrument_ids)[0] == "project"
    assert module.classify_group_name("0instrument000000000000000", user_ids, project_ids, instrument_ids)[0] == "instrument"
    assert module.classify_group_name("public", user_ids, project_ids, instrument_ids)[0] == "system"
    assert module.classify_group_name("legacy-team", user_ids, project_ids, instrument_ids)[0] == "custom"


def test_classifier_refuses_cross_entity_mfid_collision():
    module = _load_backfill_module()
    group_type, matches = module.classify_group_name("shared", set(), {"shared"}, {"shared"})
    assert group_type is None
    assert matches == ("project", "instrument")


def test_instrument_slug_backfill_obeys_new_length_contract():
    module = _load_data_script("20260810_backfill_instrument_id.py")

    assert module.slugify("AB", "0instrument000000000000000") == "0instrument00000000000000"
    assert module.slugify("A very long instrument display name", "fallback") == "a-very-long-instrument-di"
    assert len(module.deconflict("x" * 25, {"x" * 25})) == 25
    assert module.deconflict("x" * 25, {"x" * 25}).endswith("-2")


def _setup_membership_groups(db_engine):
    with Session(db_engine) as session:
        owner = User(first_name="A", last_name="Owner", email="owner@x.com", unique_id="0000-0001")
        target = User(first_name="B", last_name="Target", email="target@x.com", unique_id="0000-0002")
        session.add(owner)
        session.add(target)
        session.commit()
        session.refresh(owner)

        identity = AccessGroup(group_name=owner.unique_id, group_type=AccessGroupType.IDENTITY.value)
        system = AccessGroup(group_name="public", group_type=AccessGroupType.SYSTEM.value)
        custom = AccessGroup(group_name="legacy-team", group_type=AccessGroupType.CUSTOM.value)
        instrument = Instrument(
            unique_id="0instrument000000000000000",
            instrument_id="instrument-1",
            instrument_name="Instrument 1",
            owner="LBNL",
            location="Berkeley",
        )
        instrument_group = AccessGroup(
            group_name=instrument.unique_id,
            group_type=AccessGroupType.INSTRUMENT.value,
        )
        project = Project(project_id="project-1", organization="LBNL", project_lead_orcid=owner.unique_id)
        session.add(project)
        session.flush()
        project_group = AccessGroup(
            group_name=project.unique_id,
            group_type=AccessGroupType.PROJECT.value,
        )
        session.add(identity)
        session.add(system)
        session.add(custom)
        session.add(instrument)
        session.add(instrument_group)
        session.add(project_group)
        session.commit()
        session.refresh(identity)
        session.add(UserAccessGroupLink(
            user_id=owner.id,
            accessgroup_id=identity.id,
            role=Role.OWNER.db_value,
        ))
        session.commit()


def test_generic_membership_routes_protect_typed_groups(client, db_engine, auth_as):
    _setup_membership_groups(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    assert client.post("/users/0000-0002/access_groups/0000-0001").status_code == 409
    assert client.delete("/users/0000-0001/access_groups/0000-0001").status_code == 409
    assert client.post("/users/0000-0002/access_groups/public").status_code == 409

    with Session(db_engine) as session:
        project = session.exec(select(Project).where(Project.project_id == "project-1")).one()
    assert client.post(f"/users/0000-0002/access_groups/{project.unique_id}").status_code == 400


def test_generic_membership_post_does_not_create_unknown_group(client, db_engine, auth_as):
    _setup_membership_groups(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.post("/users/0000-0002/access_groups/misspelled-group")
    assert response.status_code == 404
    with Session(db_engine) as session:
        assert session.exec(
            select(AccessGroup).where(AccessGroup.group_name == "misspelled-group")
        ).one_or_none() is None


def test_generic_instrument_membership_rejects_humans(client, db_engine, auth_as):
    _setup_membership_groups(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    instrument_mfid = "0instrument000000000000000"

    response = client.post(f"/users/0000-0002/access_groups/{instrument_mfid}")
    assert response.status_code == 409
    with Session(db_engine) as session:
        assert session.exec(
            select(UserAccessGroupLink)
            .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
            .where(AccessGroup.group_name == instrument_mfid)
        ).first() is None


def test_generic_instrument_membership_still_accepts_service_accounts(client, db_engine, auth_as):
    _setup_membership_groups(db_engine)
    with Session(db_engine) as session:
        session.add(User(first_name="S", last_name="A", unique_id="sa-0001", is_service_account=True))
        session.commit()
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    instrument_mfid = "0instrument000000000000000"

    response = client.post(f"/users/sa-0001/access_groups/{instrument_mfid}")
    assert response.status_code == 200, response.text
    with Session(db_engine) as session:
        link = session.exec(
            select(UserAccessGroupLink)
            .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
            .join(User, User.id == UserAccessGroupLink.user_id)
            .where(AccessGroup.group_name == instrument_mfid, User.unique_id == "sa-0001")
        ).one()
        assert link.role == Role.CONTRIBUTOR.db_value


def test_generic_membership_mutations_are_deprecated(client):
    route = client.get("/openapi.json").json()["paths"]["/users/{user_ref}/access_groups/{group_name}"]
    assert route["post"]["deprecated"] is True
    assert route["delete"]["deprecated"] is True
