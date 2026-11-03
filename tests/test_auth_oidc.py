import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from authlib.oauth2.rfc6749 import OAuth2Error
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey, RSAKey

from app.auth.oidc import InvalidSignIn, OidcClient, authorization_url, orcid_endpoints, verify_id_token

ISSUER = "https://orcid.example"
KEY = RSAKey.generate_key(2048, parameters={"kid": "signing"})
JWKS = {"keys": [KEY.as_dict(private=False)]}
CLIENT = OidcClient(
    issuer=ISSUER,
    client_id="client-id",
    client_secret="client-secret",
    redirect_uri="https://api.example/auth",
    scope="openid",
    endpoints=orcid_endpoints(ISSUER),
    token_auth_method="client_secret_post",
)
REJECTED = (InvalidSignIn, JoseError, OAuth2Error, ValueError, KeyError)


def claims(**changes):
    now = int(time.time())
    values = {"iss": ISSUER, "aud": "client-id", "sub": "0000-0001", "nonce": "the-nonce", "iat": now, "exp": now + 300, "given_name": "Ada", "family_name": "Lovelace", "auth_time": now - 10}
    values.update(changes)
    return {k: v for k, v in values.items() if v is not None}


def sign(values, key=KEY, alg="RS256"):
    return jwt.encode({"alg": alg, "kid": "signing"}, values, key, algorithms=[alg])


def test_valid_token_gives_the_verified_identity():
    identity = verify_id_token(CLIENT, sign(claims()), JWKS, "the-nonce")
    assert identity.issuer == ISSUER
    assert identity.subject == "0000-0001"
    assert identity.given_name == "Ada"
    assert identity.family_name == "Lovelace"
    assert isinstance(identity.auth_time, datetime) and identity.auth_time.tzinfo == timezone.utc


@pytest.mark.parametrize("changes", [
    {"iss": "https://other.example"},
    {"iss": None},
    {"aud": "someone-else"},
    {"nonce": "another-nonce"},
    {"nonce": None},
    {"exp": int(time.time()) - 3600},
    {"sub": None},
    {"sub": ""},
], ids=["wrong-iss", "no-iss", "wrong-aud", "wrong-nonce", "no-nonce", "expired", "no-sub", "empty-sub"])
def test_bad_claims_are_refused(changes):
    with pytest.raises(REJECTED):
        verify_id_token(CLIENT, sign(claims(**changes)), JWKS, "the-nonce")


def test_token_signed_by_another_key_is_refused():
    other = RSAKey.generate_key(2048, parameters={"kid": "signing"})
    with pytest.raises(REJECTED):
        verify_id_token(CLIENT, sign(claims(), key=other), JWKS, "the-nonce")


def test_hs256_signed_with_the_client_secret_is_refused():
    token = jwt.encode({"alg": "HS256"}, claims(), OctKey.import_key("client-secret" * 4), algorithms=["HS256"])
    with pytest.raises(REJECTED):
        verify_id_token(CLIENT, token, JWKS, "the-nonce")


def test_unsigned_token_is_refused():
    import base64
    import json

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    token = f"{part({'alg': 'none'})}.{part(claims())}."
    with pytest.raises(REJECTED):
        verify_id_token(CLIENT, token, JWKS, "the-nonce")


def test_authorization_url_carries_the_request():
    url = authorization_url(CLIENT, "the-state", "the-nonce")
    assert url.startswith(f"{ISSUER}/oauth/authorize?")
    query = parse_qs(urlparse(url).query)
    assert query["state"] == ["the-state"]
    assert query["nonce"] == ["the-nonce"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://api.example/auth"]
    assert query["scope"] == ["openid"]
    assert query["response_type"] == ["code"]
    assert "max_age" not in query and "prompt" not in query


def test_max_age_zero_also_forces_login():
    query = parse_qs(urlparse(authorization_url(CLIENT, "s", "n", max_age=0)).query)
    assert query["max_age"] == ["0"]
    assert query["prompt"] == ["login"]
    query = parse_qs(urlparse(authorization_url(CLIENT, "s", "n", max_age=300)).query)
    assert query["max_age"] == ["300"]
    assert "prompt" not in query


def test_orcid_endpoints():
    endpoints = orcid_endpoints("https://orcid.org")
    assert endpoints.authorization == "https://orcid.org/oauth/authorize"
    assert endpoints.token == "https://orcid.org/oauth/token"
    assert endpoints.jwks == "https://orcid.org/oauth/jwks"
