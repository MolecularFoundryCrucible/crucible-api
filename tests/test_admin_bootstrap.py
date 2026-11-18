import json
import sys

import pytest
from sqlmodel import Session, select

from app.cli.__main__ import main
from app.auth.api_keys import key_hash
from app.tables import AccessGroup, ApiKey, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PlatformRole, Role


def _create(*args):
    return main(["admin", "create", "--no-input", *args])


def _admins(engine):
    with Session(engine) as session:
        return session.exec(select(User).where(User.platform_role == PlatformRole.ADMIN.value)).all()


def test_create_admin_creates_user_group_and_key(db_engine, capsys):
    assert _create("--first-name", "Ada", "--last-name", "Lovelace", "--email", " Ada@Lab.org ") == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"unique_id", "username", "api_key"}
    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == output["unique_id"])).one()
        assert user.platform_role == PlatformRole.ADMIN.value
        assert user.username == output["username"] == "ada_lovelace"
        assert user.email == "ada@lab.org"
        assert user.is_service_account is False
        group = session.exec(select(AccessGroup).where(AccessGroup.group_name == user.unique_id)).one()
        assert group.group_type == AccessGroupType.IDENTITY.value
        link = session.exec(select(UserAccessGroupLink).where(UserAccessGroupLink.accessgroup_id == group.id)).one()
        assert (link.user_id, link.role) == (user.id, Role.OWNER.db_value)
        assert [key.key_hash for key in session.exec(select(ApiKey).where(ApiKey.user_id == user.id)).all()] == [key_hash(output["api_key"])]


def test_orcid_becomes_the_identifier(db_engine, capsys):
    assert _create("--first-name", "A", "--last-name", "B", "--orcid", "0000-0002-1825-0097", "--username", "orcid-admin") == 0
    assert json.loads(capsys.readouterr().out)["unique_id"] == "0000-0002-1825-0097"


def test_second_admin_refused(db_engine, capsys):
    assert _create("--first-name", "A", "--last-name", "One") == 0
    assert _create("--first-name", "B", "--last-name", "Two") == 1
    assert "--allow-additional" in capsys.readouterr().err
    assert len(_admins(db_engine)) == 1


def test_allow_additional(db_engine):
    assert _create("--first-name", "A", "--last-name", "One") == 0
    assert _create("--first-name", "B", "--last-name", "Two", "--allow-additional") == 0
    assert len(_admins(db_engine)) == 2


@pytest.mark.parametrize("args", [
    ("--orcid", "not-an-orcid"),
    ("--username", "Bad Name!"),
    ("--last-name", "  "),
])
def test_invalid_values_create_nothing(db_engine, args):
    values = {"--first-name": "A", "--last-name": "B"}
    values.update(dict([args]))
    flat = [item for pair in values.items() for item in pair]
    assert _create(*flat) == 1
    with Session(db_engine) as session:
        assert session.exec(select(User)).all() == []


def test_missing_names_without_a_terminal_fail(db_engine, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["admin", "create", "--first-name", "A"]) == 1
    assert "--last-name" in capsys.readouterr().err
    assert _admins(db_engine) == []


def test_taken_email_refused(db_engine, capsys):
    assert _create("--first-name", "A", "--last-name", "One", "--email", "a@lab.org") == 0
    assert _create("--first-name", "B", "--last-name", "Two", "--email", "A@lab.org", "--allow-additional") == 1
    assert "email is already in use" in capsys.readouterr().err.lower()
    assert len(_admins(db_engine)) == 1


def _terminal(monkeypatch, answers):
    replies = iter(answers)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))


def test_terminal_prompts_for_missing_values(db_engine, monkeypatch, capsys):
    _terminal(monkeypatch, ["Grace", "", "Hopper", "", "", "", "y"])
    assert main(["admin", "create"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["username"] == "grace_hopper"
    assert len(_admins(db_engine)) == 1


def test_terminal_declined_creates_nothing(db_engine, monkeypatch):
    _terminal(monkeypatch, ["Grace", "Hopper", "", "", "", "n"])
    assert main(["admin", "create"]) == 1
    assert _admins(db_engine) == []


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_terminal_interrupted_creates_nothing(db_engine, monkeypatch, capsys, interruption):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def interrupted(prompt=""):
        raise interruption

    monkeypatch.setattr("builtins.input", interrupted)
    assert main(["admin", "create"]) == 1
    assert "nothing created" in capsys.readouterr().err
    assert _admins(db_engine) == []


def test_user_and_key_are_created_together(db_engine, monkeypatch):
    from app.auth import api_keys

    def failing(*args, **kwargs):
        raise RuntimeError("key storage failed")

    monkeypatch.setattr(api_keys, "create_key", failing)
    with pytest.raises(RuntimeError):
        _create("--first-name", "A", "--last-name", "B")
    with Session(db_engine) as session:
        assert session.exec(select(User)).all() == []
        assert session.exec(select(AccessGroup)).all() == []


def test_admin_service_account_does_not_block_the_first_person(db_engine):
    with Session(db_engine) as session:
        session.add(User(unique_id="sa-1", first_name="", last_name="", is_service_account=True, platform_role=PlatformRole.ADMIN.value))
        session.commit()
    assert _create("--first-name", "A", "--last-name", "B") == 0


def test_taken_identifier_refused(db_engine, capsys):
    assert _create("--first-name", "A", "--last-name", "One", "--orcid", "0000-0002-1825-0097") == 0
    assert _create("--first-name", "B", "--last-name", "Two", "--orcid", "0000-0002-1825-0097", "--allow-additional") == 1
    assert "0000-0002-1825-0097 already exists" in capsys.readouterr().err
    assert len(_admins(db_engine)) == 1
