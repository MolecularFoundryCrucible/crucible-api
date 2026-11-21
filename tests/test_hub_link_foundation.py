from urllib.parse import parse_qs, urlparse

import pytest

from app.api.routes.browser_sign_in import LOGIN_COOKIE, login_cookie_name
import tests.test_hub_tokens as hub_tokens_tests
from tests.test_hub_tokens import HUB_LOGIN, hub_error_of

engine = hub_tokens_tests.engine
hub_routes = hub_tokens_tests.hub_routes

pytestmark = pytest.mark.usefixtures("hub_client_registered")


def begin(client, hub_state):
    started = client.get("/oauth/authorize", params={**HUB_LOGIN, "state": hub_state}, follow_redirects=False)
    assert started.status_code == 302
    query = parse_qs(urlparse(started.headers["location"]).query)
    return query["state"][0], query["nonce"][0]


def finish(client, fake, state, nonce, **extra):
    fake.claims = {"nonce": nonce}
    return client.get("/oauth/callback/orcid", params={"code": "orcid-code", "state": state, **extra}, follow_redirects=False)


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
    assert client.get("/oauth/callback/orcid", params={"code": "x", "state": "forged-state"}, follow_redirects=False).status_code == 400
    assert login_cookies(client) == {login_cookie_name(state)}
    assert "code" in returned_code(finish(client, fake, state, nonce))


def test_response_from_another_issuer_is_refused(hub_routes):
    client, fake = hub_routes
    state, nonce = begin(client, "mix-up-attack-state1")
    assert hub_error_of(finish(client, fake, state, nonce, iss="https://login.attacker.example")) == "access_denied"


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

    from app.tables import TokenFamily
    from app.utils import auth_utils
    from app.auth.tokens import issue_tokens
    from tests.people import internal_id

    use_engine(engine)
    monkeypatch.setattr(auth_utils, "_PUBLIC_GROUP_ID", None)
    pair = issue_tokens(engine, internal_id(engine, "0000-0000-0000-0001"), "crucible-hub")
    principal = auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=pair.access_token))
    with Session(engine) as session:
        family = session.exec(select(TokenFamily)).one()
    assert principal.session_family_id == family.id
