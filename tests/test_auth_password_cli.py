import json
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import Session, SQLModel, select

from app import config
from app.auth.password_links import issue_link, live_link
from app.auth.passwords import store_password
from app.cli import admin as admin_cli
from app.cli import auth as auth_cli
from app.tables import AuthEvent, HubAccessToken, HubRefreshToken, HubTokenFamily, LocalCredential, PasswordToken, User, UserIdentity
from app.utils.hub_tokens import issue_hub_tokens

LAB = "https://login.lab.example/realms/crucible"
NEW_LAB = "https://sso.lab.example/realms/crucible"
IDS = {"ada": "0tmnyhfztdwc1000w2kspzsnh0", "bob": "0tmnyhfztdwc1000w2kspzsnh1", "cy": "0tmnyhfztdwc1000w2kspzsnh2"}


@pytest.fixture
def engine(db_engine, use_engine, monkeypatch):
    for name, value in {
        "CRUCIBLE_AUTH_PASSWORD_ENABLED": "true", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one",
        "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "HUB_AUTH_PUBLIC_API_URL": "http://localhost:7777",
        "CRUCIBLE_AUTH_PROVIDERS": json.dumps([
            {"key": "lab", "display_name": "Lab", "issuer": LAB, "client_id": "c", "scopes": "openid", "token_endpoint_auth_method": "client_secret_basic", "registration": "closed", "legacy_callback": False},
            {"key": "newlab", "display_name": "New lab", "issuer": NEW_LAB, "client_id": "c", "scopes": "openid", "token_endpoint_auth_method": "client_secret_basic", "registration": "closed", "legacy_callback": False},
        ]),
    }.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    SQLModel.metadata.create_all(db_engine, tables=[HubTokenFamily.__table__, HubRefreshToken.__table__, HubAccessToken.__table__])
    use_engine(db_engine)
    return db_engine


def add_person(engine, username, *, password=None, issuer=None, subject=None):
    with Session(engine) as session:
        user = User(unique_id=IDS[username], username=username, first_name=username, last_name="X")
        session.add(user)
        session.commit()
        if password:
            store_password(session, user, password)
        if issuer:
            session.add(UserIdentity(user_id=user.id, issuer=issuer, subject=subject or username))
        session.commit()
        return user.id


def run(module, handler, **values):
    return getattr(module, handler)(type("Args", (), values)())


def test_admin_create_prints_a_setup_link_when_passwords_are_on(engine, capsys, monkeypatch):
    assert run(admin_cli, "_run_create", first_name="Ada", last_name="Lovelace", email="ada@example.org", orcid=None, username="ada", allow_additional=False, no_input=True) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["api_key"] and created["password_link_expires_at"]
    token = parse_qs(urlparse(created["password_link"]).query)["token"][0]
    link = live_link(engine, token)
    assert link is not None and link.purpose == "setup" and link.username == "ada"
    monkeypatch.setenv("CRUCIBLE_AUTH_PASSWORD_ENABLED", "false")
    config.get_settings.cache_clear()
    assert run(admin_cli, "_run_create", first_name="Bob", last_name="B", email=None, orcid=None, username="bob", allow_additional=True, no_input=True) == 0
    assert "password_link" not in json.loads(capsys.readouterr().out)


def test_revoke_passwords_counts_first_then_removes_every_password(engine, capsys):
    ada = add_person(engine, "ada", password="an old password nobody remembers")
    bob = add_person(engine, "bob", password="another old password for bob", issuer=LAB, subject="bob-at-lab")
    cy = add_person(engine, "cy", issuer=LAB, subject="cy-at-lab")
    for person in (ada, bob, cy):
        issue_hub_tokens(engine, person, "crucible-hub")
    with Session(engine) as session:
        issue_link(session, session.get(User, ada), "reset", actor=None)
        session.commit()
    assert run(auth_cli, "_run_revoke_passwords", commit=False) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "dry-run", "passwords": 2, "without_another_login": 1}
    with Session(engine) as session:
        assert len(session.exec(select(LocalCredential)).all()) == 2
    assert run(auth_cli, "_run_revoke_passwords", commit=True) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "commit"
    with Session(engine) as session:
        assert session.exec(select(LocalCredential)).all() == []
        assert session.exec(select(PasswordToken)).one().voided_at is not None
        revoked = {family.user_id: family.revoked_at for family in session.exec(select(HubTokenFamily)).all()}
        assert revoked[ada] and revoked[bob] and revoked[cy] is None
        assert sorted(session.exec(select(AuthEvent.user_id).where(AuthEvent.event == "password_removed")).all()) == sorted([ada, bob])


def test_rewrite_issuer_moves_logins_only_to_a_listed_provider_without_collisions(engine, capsys):
    add_person(engine, "ada", issuer=LAB, subject="ada-at-lab")
    add_person(engine, "bob", issuer=LAB, subject="bob-at-lab")
    assert run(auth_cli, "_run_rewrite_issuer", old=LAB, new="https://unlisted.example", commit=True) == 1
    assert run(auth_cli, "_run_rewrite_issuer", old=LAB, new=LAB, commit=True) == 1
    capsys.readouterr()
    assert run(auth_cli, "_run_rewrite_issuer", old=LAB, new=NEW_LAB, commit=False) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "dry-run", "logins": 2}
    with Session(engine) as session:
        assert set(session.exec(select(UserIdentity.issuer)).all()) == {LAB}
    assert run(auth_cli, "_run_rewrite_issuer", old=LAB, new=NEW_LAB, commit=True) == 0
    with Session(engine) as session:
        assert set(session.exec(select(UserIdentity.issuer)).all()) == {NEW_LAB}


def test_rewrite_issuer_changes_nothing_when_a_login_already_exists_under_the_new_issuer(engine, capsys):
    add_person(engine, "ada", issuer=LAB, subject="same-subject")
    add_person(engine, "bob", issuer=NEW_LAB, subject="same-subject")
    assert run(auth_cli, "_run_rewrite_issuer", old=LAB, new=NEW_LAB, commit=True) == 1
    assert "already linked" in capsys.readouterr().err
    with Session(engine) as session:
        assert sorted(session.exec(select(UserIdentity.issuer)).all()) == sorted([LAB, NEW_LAB])
