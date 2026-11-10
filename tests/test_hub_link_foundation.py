import importlib.util
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.api.routes.hub_auth import LOGIN_COOKIE, login_cookie_name
import tests.test_hub_tokens as hub_tokens_tests
from tests.test_hub_tokens import HUB_LOGIN, hub_error_of

engine = hub_tokens_tests.engine
hub_routes = hub_tokens_tests.hub_routes

pytestmark = pytest.mark.usefixtures("hub_client_registered")


def begin(client, hub_state):
    started = client.get("/auth/hub/login", params={**HUB_LOGIN, "state": hub_state}, follow_redirects=False)
    assert started.status_code == 302
    query = parse_qs(urlparse(started.headers["location"]).query)
    return query["state"][0], query["nonce"][0]


def finish(client, fake, state, nonce, **extra):
    fake.claims = {"nonce": nonce}
    return client.get("/auth/hub/callback", params={"code": "orcid-code", "state": state, **extra}, follow_redirects=False)


def login_cookies(client):
    return {cookie.name for cookie in client.cookies.jar if cookie.name.startswith(LOGIN_COOKIE)}


def returned_code(response):
    assert response.status_code == 302, response.text
    return parse_qs(urlparse(response.headers["location"]).query)


def test_parallel_sign_ins_keep_their_own_cookies(hub_routes):
    client, fake = hub_routes
    first = begin(client, "first-tab-state-0001")
    second = begin(client, "second-tab-state-002")
    assert login_cookies(client) == {login_cookie_name(first[0]), login_cookie_name(second[0])}
    assert returned_code(finish(client, fake, *second))["state"] == ["second-tab-state-002"]
    assert login_cookies(client) == {login_cookie_name(first[0])}
    assert returned_code(finish(client, fake, *first))["state"] == ["first-tab-state-0001"]
    assert login_cookies(client) == set()


def test_unmatched_callback_leaves_the_sign_in_alone(hub_routes):
    client, fake = hub_routes
    state, nonce = begin(client, "the-only-tab-state-1")
    assert client.get("/auth/hub/callback", params={"code": "x", "state": "forged-state"}, follow_redirects=False).status_code == 400
    assert login_cookies(client) == {login_cookie_name(state)}
    assert "code" in returned_code(finish(client, fake, state, nonce))


def test_sign_in_started_on_an_older_pod_still_finishes(hub_routes):
    client, fake = hub_routes
    state, nonce = begin(client, "older-pod-tab-state1")
    secret = client.cookies.get(login_cookie_name(state))
    client.cookies.clear()
    client.cookies.set(LOGIN_COOKIE, secret, path="/auth/hub")
    assert "code" in returned_code(finish(client, fake, state, nonce))


def test_response_from_another_issuer_is_refused(hub_routes):
    client, fake = hub_routes
    state, nonce = begin(client, "mix-up-attack-state1")
    assert hub_error_of(finish(client, fake, state, nonce, iss="https://login.attacker.example")) == "access_denied"


def test_matching_issuer_is_accepted(hub_routes):
    client, fake = hub_routes
    state, nonce = begin(client, "matching-iss-state01")
    assert "code" in returned_code(finish(client, fake, state, nonce, iss="https://orcid.org"))


def test_provider_that_advertises_iss_must_send_it(hub_routes):
    client, fake = hub_routes
    fake.metadata["authorization_response_iss_parameter_supported"] = True
    state, nonce = begin(client, "advertised-iss-state")
    assert hub_error_of(finish(client, fake, state, nonce)) == "access_denied"
    state, nonce = begin(client, "advertised-iss-ok-st")
    assert "code" in returned_code(finish(client, fake, state, nonce, iss="https://orcid.org"))


def test_hub_principal_carries_its_token_family(engine, use_engine, monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials
    from sqlmodel import Session, select

    from app.tables import HubTokenFamily
    from app.utils import auth_utils
    from app.utils.hub_tokens import issue_hub_tokens
    from tests.people import internal_id

    use_engine(engine)
    monkeypatch.setattr(auth_utils, "_PUBLIC_GROUP_ID", None)
    pair = issue_hub_tokens(engine, internal_id(engine, "0000-0000-0000-0001"), "crucible-hub")
    principal = auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=pair.access_token))
    with Session(engine) as session:
        family = session.exec(select(HubTokenFamily)).one()
    assert principal.session_family_id == family.id


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_hub_link_columns_migration_keeps_sign_ins_working():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261106_a8c2e5f1d903_hub_link_columns.py"
    spec = importlib.util.spec_from_file_location("hub_link_columns_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    pg = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_hub_link_columns_" + uuid4().hex
    with pg.connect() as connection:
        transaction = connection.begin()
        try:
            run = lambda sql: connection.execute(text(sql))  # noqa: E731
            run(f'CREATE SCHEMA "{schema}"')
            run(f'SET LOCAL search_path TO "{schema}"')
            run('CREATE TABLE "user" (id serial PRIMARY KEY)')
            run("CREATE TABLE hublogintransaction (state_hash varchar(64) PRIMARY KEY, browser_secret_hash varchar(64) NOT NULL)")
            run("CREATE TABLE hublogincode (code_hash varchar(64) PRIMARY KEY)")
            run("INSERT INTO hublogintransaction VALUES ('in-flight', 'secret')")
            run("INSERT INTO hublogincode VALUES ('issued')")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT purpose FROM hublogintransaction")).scalar() == "sign_in"
                assert connection.execute(text("SELECT purpose FROM hublogincode")).scalar() == "sign_in"
                run("INSERT INTO hublogintransaction (state_hash) VALUES ('older-pod-style')")
                run("INSERT INTO hublogintransaction (state_hash, purpose, start_hash) VALUES ('link', 'link', 'start')")
                run("INSERT INTO hublogincode (code_hash, purpose) VALUES ('link-code', 'link')")
                migration.downgrade()
                assert connection.execute(text("SELECT state_hash FROM hublogintransaction ORDER BY state_hash")).scalars().all() == ["in-flight"]
                assert connection.execute(text("SELECT code_hash FROM hublogincode")).scalars().all() == ["issued"]
                assert "purpose" not in {c["name"] for c in inspect(connection).get_columns("hublogintransaction", schema=schema)}
        finally:
            transaction.rollback()
    pg.dispose()
