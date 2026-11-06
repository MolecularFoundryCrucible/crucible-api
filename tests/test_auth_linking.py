import json
import base64
import hashlib
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import Session, SQLModel, select

from app.api.main import app
from app.models.core_models import AuthEvent, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, User, UserIdentity
from app.utils.auth_utils import authenticate_user
from app.utils.hub_tokens import issue_hub_tokens
from tests.test_auth_providers import remote  # noqa: F401

ORCID_ISSUER = "https://orcid.org"
LAB_ISSUER = "https://login.example.org/realms/lab"
ADA = "0tmnyhfztdwc1000w2kspzsnh0"
BOB = "0tmnyhfztdwc1000w2kspzsnh1"
ADA_ORCID = "0000-0002-1825-0097"
BOB_ORCID = "0000-0001-5109-3700"
SIGN_IN_CALLBACK = "https://crucible.lbl.gov/hub/auth/callback"
LINK_CALLBACK = "https://crucible.lbl.gov/hub/auth/link/callback"
HUB = ("crucible-hub", "test-only-secret-padded-to-32-characters")
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def world(remote, client, db_engine, monkeypatch):  # noqa: F811
    import app.api.routes.hub_auth as hub_routes

    for name, value in {"HUB_AUTH_CALLBACK_URI": f"{SIGN_IN_CALLBACK},{LINK_CALLBACK}", "HUB_AUTH_PUBLIC_API_URL": "http://localhost:7777",
                        "HUB_AUTH_ENABLED": "true", "HUB_AUTH_CLIENT_SECRET": HUB[1], "HUB_AUTH_CLIENT_ID": HUB[0], "CRUCIBLE_AUTH_IDENTITY_LOOKUP": "true"}.items():
        monkeypatch.setenv(name, value)
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr(hub_routes, "get_api_root_path", lambda: "")
    app.dependency_overrides.pop(authenticate_user, None)
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginTransaction, HubLoginCode, HubTokenFamily, HubRefreshToken, HubAccessToken)])
    with Session(db_engine) as session:
        for unique_id, orcid in ((ADA, ADA_ORCID), (BOB, BOB_ORCID)):
            user = User(unique_id=unique_id, username=unique_id[-4:], first_name="A", last_name="B", email=None)
            session.add(user)
            session.flush()
            session.add(UserIdentity(user_id=user.id, issuer=ORCID_ISSUER, subject=orcid, verified_at=datetime.now(timezone.utc)))
        session.commit()
    return remote, client, db_engine


def user_id(engine, unique_id):
    with Session(engine) as session:
        return session.exec(select(User.id).where(User.unique_id == unique_id)).one()


def hub_session(engine, unique_id, *, age=timedelta(seconds=5)):
    return issue_hub_tokens(engine, user_id(engine, unique_id), "crucible-hub", auth_time=datetime.now(timezone.utc) - age).access_token


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def provider_sign_in(world, start_url, *, subject, provider="lab", auth_time=0, email="ada@lab.example", keep_cookie=True):
    fakes, client, _ = world
    opened = client.get(urlparse(start_url).path + "?" + urlparse(start_url).query, follow_redirects=False)
    assert opened.status_code == 302, opened.text
    query = parse_qs(urlparse(opened.headers["location"]).query)
    assert (query["max_age"], query["prompt"]) == (["0"], ["login"])
    fake = fakes["login.example.org" if provider == "lab" else "orcid.org"]
    fake.claims = {"nonce": query["nonce"][0], "sub": subject, "email": email, **({} if auth_time is None else {"auth_time": int(time.time()) - auth_time})}
    if not keep_cookie:
        client.cookies.clear()
    path = "/auth/hub/oidc/lab/callback" if provider == "lab" else "/auth/hub/callback"
    return client.get(path, params={"code": "provider-code", "state": query["state"][0]}, follow_redirects=False)


def start_link(world, session_token, provider="lab", hub_state="link-hub-state-0001"):
    _, client, _ = world
    started = client.post(f"/account/identities/{provider}/link", headers=bearer(session_token), json={"callback_uri": LINK_CALLBACK, "state": hub_state, "code_challenge": CHALLENGE})
    assert started.status_code == 200, started.text
    return started.json()["authorization_url"]


def link_code(response):
    assert response.status_code == 302, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert response.headers["location"].startswith(LINK_CALLBACK)
    return query["code"][0]


def code_body(code, session_token):
    return {"code": code, "code_verifier": VERIFIER, "redirect_uri": LINK_CALLBACK, "session_token": session_token}


def linked(world, issuer=LAB_ISSUER):
    with Session(world[2]) as session:
        return [(row.user_id, row.subject, row.verified_at is not None) for row in session.exec(select(UserIdentity).where(UserIdentity.issuer == issuer)).all()]


def test_link_adds_a_verified_login_after_a_preview(world):
    _, client, engine = world
    ada = hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, ada), subject="ada-lab"))
    preview = client.post("/oauth/link/preview", auth=HUB, json=code_body(code, ada))
    assert preview.status_code == 200, preview.text
    assert {key: preview.json()[key] for key in ("provider_key", "provider_name", "label", "orcid", "status")} == {"provider_key": "lab", "provider_name": "Example Lab", "label": "ada@lab.example", "orcid": None, "status": "available"}
    assert linked(world) == []
    confirmed = client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada))
    assert confirmed.status_code == 200, confirmed.text
    assert linked(world) == [(user_id(engine, ADA), "ada-lab", True)]
    listed = client.get("/account/identities", headers=bearer(ada)).json()
    assert [(item["provider_key"], item["label"], item["orcid"]) for item in listed] == [("orcid", None, ADA_ORCID), ("lab", "ada@lab.example", None)]
    with Session(engine) as session:
        assert session.exec(select(AuthEvent.event)).all() == ["identity_linked"]


def test_victim_opening_attackers_link_address_writes_nothing(world):
    _, client, engine = world
    attacker, victim = hub_session(engine, BOB), hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, attacker), subject="victim-lab"))
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, victim)).status_code == 401
    assert linked(world) == []


def test_attackers_login_cannot_be_linked_to_victims_account(world):
    _, client, engine = world
    attacker, victim = hub_session(engine, BOB), hub_session(engine, ADA)
    start_link(world, victim, hub_state="victims-hub-state-01")
    attackers_code = link_code(provider_sign_in(world, start_link(world, attacker, hub_state="attacker-hub-state1"), subject="attacker-lab"))
    assert client.post("/oauth/link/preview", auth=HUB, json=code_body(attackers_code, victim)).status_code == 401
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(attackers_code, victim)).status_code == 401
    assert linked(world) == []


def test_link_callback_without_the_browser_cookie_refused(world):
    _, _, engine = world
    response = provider_sign_in(world, start_link(world, hub_session(engine, ADA)), subject="ada-lab", keep_cookie=False)
    assert response.status_code == 400


def test_start_address_is_single_use(world):
    _, client, engine = world
    start = start_link(world, hub_session(engine, ADA))
    path = urlparse(start).path + "?" + urlparse(start).query
    assert client.get(path, follow_redirects=False).status_code == 302
    assert client.get(path, follow_redirects=False).status_code == 400


def test_link_code_is_single_use_and_cannot_sign_in(world):
    _, client, engine = world
    ada = hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, ada), subject="ada-lab"))
    assert client.post("/auth/hub/token", auth=HUB, json={"code": code, "code_verifier": VERIFIER, "redirect_uri": LINK_CALLBACK}).status_code == 401
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada)).status_code == 200
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada)).status_code == 401


def test_sign_in_code_cannot_confirm_a_link(world):
    fakes, client, engine = world
    started = client.get("/auth/hub/login", params={"client_id": "crucible-hub", "redirect_uri": SIGN_IN_CALLBACK, "state": "sign-in-state-00001", "code_challenge": CHALLENGE,
                                                    "code_challenge_method": "S256", "provider": "orcid"}, follow_redirects=False)
    query = parse_qs(urlparse(started.headers["location"]).query)
    fakes["orcid.org"].claims = {"nonce": query["nonce"][0], "sub": ADA_ORCID}
    finished = client.get("/auth/hub/callback", params={"code": "c", "state": query["state"][0]}, follow_redirects=False)
    sign_in_code = parse_qs(urlparse(finished.headers["location"]).query)["code"][0]
    assert client.post("/oauth/link/confirm", auth=HUB, json={**code_body(sign_in_code, hub_session(engine, ADA)), "redirect_uri": SIGN_IN_CALLBACK}).status_code == 401


def test_api_key_and_stale_session_are_refused(world, auth_as):
    _, client, engine = world
    stale = hub_session(engine, ADA, age=timedelta(hours=1))
    body = {"callback_uri": LINK_CALLBACK, "state": "link-hub-state-0001", "code_challenge": CHALLENGE}
    assert client.post("/account/identities/lab/link", headers=bearer(stale), json=body).json()["detail"]["code"] == "insufficient_user_authentication"
    assert client.delete("/account/identities/1", headers=bearer(stale)).status_code == 401
    auth_as(ADA)
    assert client.post("/account/identities/lab/link", json=body).json()["detail"]["code"] == "hub_session_required"
    assert client.delete("/account/identities/1").json()["detail"]["code"] == "hub_session_required"


def test_provider_must_have_asked_to_sign_in_again(world):
    _, _, engine = world
    ada = hub_session(engine, ADA)
    for auth_time in (3600, None):
        refused = provider_sign_in(world, start_link(world, ada), subject="ada-lab", auth_time=auth_time)
        assert parse_qs(urlparse(refused.headers["location"]).query)["error"] == ["login_required"]


@pytest.mark.parametrize("subject, provider, status", [
    ("bob-lab", "lab", "linked_elsewhere"),
    (BOB_ORCID, "orcid", "linked_elsewhere"),
    (ADA_ORCID, "orcid", "already_yours"),
])
def test_preview_names_the_status(world, subject, provider, status):
    _, client, engine = world
    with Session(engine) as session:
        session.add(UserIdentity(user_id=user_id(engine, BOB), issuer=LAB_ISSUER, subject="bob-lab"))
        session.commit()
    ada = hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, ada, provider), subject=subject, provider=provider))
    assert client.post("/oauth/link/preview", auth=HUB, json=code_body(code, ada)).json()["status"] == status
    confirm = client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada))
    assert confirm.status_code == (200 if status == "already_yours" else 409)


def test_second_orcid_and_another_persons_unique_id_are_refused(world):
    _, client, engine = world
    with Session(engine) as session:
        session.add(User(unique_id="0000-0003-0000-0003", username="carl", first_name="C", last_name="D", email=None))
        session.commit()
    ada = hub_session(engine, ADA)
    for subject, status in (("0000-0004-0000-0004", "second_orcid"), ("0000-0003-0000-0003", "second_orcid")):
        code = link_code(provider_sign_in(world, start_link(world, ada, "orcid"), subject=subject, provider="orcid"))
        assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada)).json()["detail"]["code"] == status


def test_orcid_of_another_person_is_refused_for_someone_without_an_orcid(world):
    _, client, engine = world
    with Session(engine) as session:
        session.add(User(unique_id="0000-0003-0000-0003", username="carl", first_name="C", last_name="D", email=None))
        dana = User(unique_id="0tmnyhfztdwc1000w2kspzsnh2", username="dana", first_name="D", last_name="E", email=None)
        session.add(dana)
        session.flush()
        session.add(UserIdentity(user_id=dana.id, issuer=LAB_ISSUER, subject="dana-lab"))
        session.commit()
    dana = hub_session(engine, "0tmnyhfztdwc1000w2kspzsnh2")
    code = link_code(provider_sign_in(world, start_link(world, dana, "orcid"), subject="0000-0003-0000-0003", provider="orcid"))
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, dana)).json()["detail"]["code"] == "orcid_of_another"


def test_linking_an_unverified_orcid_verifies_it(world):
    _, client, engine = world
    with Session(engine) as session:
        row = session.exec(select(UserIdentity).where(UserIdentity.subject == ADA_ORCID)).one()
        row.verified_at = None
        session.add(row)
        session.commit()
    ada = hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, ada, "orcid"), subject=ADA_ORCID, provider="orcid"))
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada)).status_code == 200
    assert (user_id(engine, ADA), ADA_ORCID, True) in linked(world, ORCID_ISSUER)


def test_last_method_cannot_be_unlinked(world):
    _, client, engine = world
    ada = hub_session(engine, ADA)
    only = client.get("/account/identities", headers=bearer(ada)).json()[0]["id"]
    response = client.delete(f"/account/identities/{only}", headers=bearer(ada))
    assert (response.status_code, response.json()["detail"]["code"]) == (409, "last_sign_in_method")


def test_unlink_ends_other_sessions_keeps_this_one_and_voids_live_links(world):
    _, client, engine = world
    ada, other = hub_session(engine, ADA), hub_session(engine, ADA)
    client.post("/oauth/link/confirm", auth=HUB, json=code_body(link_code(provider_sign_in(world, start_link(world, ada), subject="ada-lab")), ada))
    pending = link_code(provider_sign_in(world, start_link(world, ada, hub_state="pending-hub-state-01"), subject="ada-second-lab", email="second@lab.example"))
    orcid_row = next(item for item in client.get("/account/identities", headers=bearer(ada)).json() if item["provider_key"] == "orcid")
    assert client.delete(f"/account/identities/{orcid_row['id']}", headers=bearer(ada)).status_code == 204
    assert client.get("/account/identities", headers=bearer(ada)).status_code == 200
    assert client.get("/account/identities", headers=bearer(other)).status_code == 401
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(pending, ada)).status_code == 401
    assert linked(world, ORCID_ISSUER) == [(user_id(engine, BOB), BOB_ORCID, True)]
    with Session(engine) as session:
        assert session.exec(select(AuthEvent.event)).all() == ["identity_linked", "identity_unlinked"]


def test_linking_waits_for_the_identity_lookup_setting(world, monkeypatch):
    _, client, engine = world
    ada = hub_session(engine, ADA)
    from app.config import get_settings
    monkeypatch.setenv("CRUCIBLE_AUTH_IDENTITY_LOOKUP", "false")
    get_settings.cache_clear()
    response = client.post("/account/identities/lab/link", headers=bearer(ada), json={"callback_uri": LINK_CALLBACK, "state": "link-hub-state-0001", "code_challenge": CHALLENGE})
    assert (response.status_code, response.json()["detail"]["code"]) == (409, "identity_lookup_off")


def test_a_link_belongs_to_the_client_whose_session_started_it(world, monkeypatch):
    _, client, engine = world
    for name in ("HUB_AUTH_ENABLED", "HUB_AUTH_CLIENT_ID", "HUB_AUTH_CALLBACK_URI", "HUB_AUTH_CLIENT_SECRET"):
        monkeypatch.delenv(name)
    clients = [{"key": key, "client_id": client_id, "display_name": key, "type": "confidential", "redirect_uris": [SIGN_IN_CALLBACK, LINK_CALLBACK]} for key, client_id in (("hub", HUB[0]), ("other", "other-app"))]
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps(clients))
    monkeypatch.setenv("OAUTH_HUB_CLIENT_SECRET", "h" * 40)
    monkeypatch.setenv("OAUTH_OTHER_CLIENT_SECRET", "o" * 40)
    from app.config import get_settings
    get_settings.cache_clear()
    hub, other = (HUB[0], "h" * 40), ("other-app", "o" * 40)
    ada = hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, ada), subject="ada-lab"))
    assert client.post("/oauth/link/preview", auth=other, json=code_body(code, ada)).status_code == 401
    other_session = issue_hub_tokens(engine, user_id(engine, ADA), "other-app", auth_time=datetime.now(timezone.utc)).access_token
    assert client.post("/oauth/link/confirm", auth=hub, json=code_body(code, other_session)).status_code == 401
    assert client.post("/oauth/link/confirm", auth=hub, json=code_body(code, ada)).status_code == 200


def test_only_the_session_that_started_a_link_can_confirm_it(world):
    _, client, engine = world
    ada = hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, ada), subject="ada-lab"))
    other_tab = hub_session(engine, ADA)
    assert client.post("/oauth/link/preview", auth=HUB, json=code_body(code, other_tab)).status_code == 401
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, other_tab)).status_code == 401
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada)).status_code == 200


def test_confirming_needs_a_recent_sign_in_again(world):
    _, client, engine = world
    ada = hub_session(engine, ADA)
    code = link_code(provider_sign_in(world, start_link(world, ada), subject="ada-lab"))
    with Session(engine) as session:
        for family in session.exec(select(HubTokenFamily)).all():
            family.auth_time = datetime.now(timezone.utc) - timedelta(minutes=11)
            session.add(family)
        session.commit()
    refused = client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada))
    assert (refused.status_code, refused.json()["detail"]["code"]) == (401, "insufficient_user_authentication")
    assert linked(world) == []


def test_start_address_expires_after_two_minutes(world):
    _, client, engine = world
    start_url = start_link(world, hub_session(engine, ADA))
    with Session(engine) as session:
        transaction = session.exec(select(HubLoginTransaction).where(HubLoginTransaction.purpose == "link")).one()
        transaction.created_at = datetime.now(timezone.utc) - timedelta(seconds=121)
        session.add(transaction)
        session.commit()
    assert client.get(urlparse(start_url).path + "?" + urlparse(start_url).query, follow_redirects=False).status_code == 400


def test_link_starts_are_limited(world):
    _, client, engine = world
    ada = hub_session(engine, ADA)
    for number in range(5):
        start_link(world, ada, hub_state=f"link-hub-state-{number:04d}")
    refused = client.post("/account/identities/lab/link", headers=bearer(ada), json={"callback_uri": LINK_CALLBACK, "state": "link-hub-state-0099", "code_challenge": CHALLENGE})
    assert (refused.status_code, refused.json()["detail"]["code"], refused.headers["retry-after"]) == (429, "too_many_links", "600")


def test_link_routes_live_only_under_oauth_and_record_the_client(world):
    _, client, engine = world
    for path in ("/auth/hub/link/preview", "/auth/hub/link/confirm"):
        assert client.post(path, auth=HUB, json={}).status_code == 404
    ada = hub_session(engine, ADA)
    start_url = start_link(world, ada)
    assert urlparse(start_url).path == "/oauth/link/start"
    code = link_code(provider_sign_in(world, start_url, subject="ada-lab"))
    assert client.post("/oauth/link/confirm", auth=HUB, json=code_body(code, ada)).status_code == 200
    with Session(engine) as session:
        assert session.exec(select(AuthEvent.client_id).where(AuthEvent.event == "identity_linked")).all() == [HUB[0]]
