"""The API's OAuth 2.0 authorization server, on Authlib's protocol classes: the authorization code
grant with PKCE, refresh with rotation, and revocation (RFC 7009). Authlib parses and checks each
request and shapes the errors; codes, token families and tokens stay in the API's own tables, and
are consumed and issued by the functions in app.utils.hub_login and app.utils.hub_tokens."""
import secrets
from collections import defaultdict
from datetime import datetime, timezone

from authlib.oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import ClientMixin, InvalidGrantError, OAuth2Request
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc7009 import RevocationEndpoint
from authlib.oauth2.rfc7636 import CodeChallenge
from sqlmodel import Session, select

from app.auth.clients import ClientUnavailable, RegisteredClient, client_by_id
from app.config import ConfigurationError, optional_secret
from app.models.core_models import HubAccessToken, HubLoginCode, HubRefreshToken, HubTokenFamily
from app.utils.hub_login import exchange_handoff_code
from app.utils.hub_tokens import ACCESS_PREFIX, REFRESH_PREFIX, HubTokenPair, InvalidHubGrant, refresh_hub_tokens, revoke_hub_tokens

AUTH_METHODS = ["client_secret_basic", "none"]


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class _FormPayload(BasicOAuth2Payload):
    def __init__(self, fields: list[tuple[str, str]]):
        super().__init__(dict(reversed(fields)))
        self._datalist = defaultdict(list)
        for name, value in fields:
            self._datalist[name].append(value)


class FormRequest(OAuth2Request):
    """A token or revocation request, from form fields and headers already read by the route. `uri`
    is the public address, since behind a proxy the request itself may arrive over plain http.
    Every value of a repeated field is kept, so Authlib can refuse repeats."""

    def __init__(self, uri: str, headers: dict[str, str], fields: list[tuple[str, str]], engine):
        super().__init__("POST", uri, headers=headers)
        self.engine = engine
        self.payload = _FormPayload(fields)
        self._form = self.payload.data
        self.token_pair: HubTokenPair | None = None

    @property
    def args(self):
        return {}

    @property
    def form(self):
        return self._form


class AuthorizeRequest(OAuth2Request):
    """An authorization request, from its query parameters, checked against the public address."""

    def __init__(self, uri: str, fields: list[tuple[str, str]]):
        super().__init__("GET", uri)
        self.payload = _FormPayload(fields)

    @property
    def args(self):
        return self.payload.data

    @property
    def form(self):
        return {}


def check_authorization_request(uri: str, fields: list[tuple[str, str]]) -> None:
    """Authlib's checks of an authorization request (RFC 6749 4.1.1 and 3.1): known client, a
    redirect on its list, response_type code, no repeated parameter, and a well-formed PKCE
    challenge. Raises Authlib's OAuth2Error, whose redirect_uri is set only when the client and the
    redirect were verified, so the error may be sent back to the client."""
    request = AuthorizeRequest(uri, fields)
    grant = authorization_server().get_authorization_grant(request)
    grant.validate_no_multiple_request_parameter(request)
    grant.validate_authorization_request()


class OAuthClient(ClientMixin):
    def __init__(self, client: RegisteredClient):
        self.client = client

    def get_client_id(self):
        return self.client.client_id

    def get_default_redirect_uri(self):
        return self.client.redirect_uris[0]

    def get_allowed_scope(self, scope):
        return ""

    def check_redirect_uri(self, redirect_uri):
        return self.client.allows_redirect(redirect_uri)

    def check_client_secret(self, client_secret):
        if self.client.is_public:
            return False
        try:
            expected = optional_secret(self.client.secret_name)
        except ConfigurationError:
            raise ClientUnavailable from None
        if not expected:
            raise ClientUnavailable
        return secrets.compare_digest(client_secret.encode("utf-8"), expected.encode("utf-8"))

    def check_endpoint_auth_method(self, method, endpoint):
        """A confidential client proves itself with HTTP Basic; a public one cannot, so it sends only
        its client_id and relies on PKCE (RFC 9700 2.1.1)."""
        return method == ("none" if self.client.is_public else "client_secret_basic")

    def check_response_type(self, response_type):
        return response_type == "code"

    def check_grant_type(self, grant_type):
        return grant_type in ("authorization_code", "refresh_token")


class _Credential:
    """What Authlib needs of a stored code or token: whose client it belongs to and its scope."""

    def __init__(self, client_id: str, user_id: int | None, *, redirect_uri: str | None = None, code_challenge: str | None = None, raw: str | None = None):
        self.client_id, self.user_id, self.redirect_uri, self.code_challenge, self.raw = client_id, user_id, redirect_uri, code_challenge, raw

    def check_client(self, client):
        return client.get_client_id() == self.client_id

    def get_redirect_uri(self):
        return self.redirect_uri

    def get_scope(self):
        return ""


def token_body(pair: HubTokenPair, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    body = {
        "access_token": pair.access_token,
        "token_type": "Bearer",
        "expires_in": max(0, int((_aware(pair.access_expires_at) - current).total_seconds())),
        "refresh_token": pair.refresh_token,
    }
    if pair.auth_time is not None:
        body["auth_time"] = int(_aware(pair.auth_time).timestamp())
    return body


class CodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = AUTH_METHODS

    def query_authorization_code(self, code, client):
        with Session(self.request.engine) as session:
            grant = session.exec(select(HubLoginCode).where(HubLoginCode.code_hash == _digest(code))).one_or_none()
        if grant is None or grant.purpose != "sign_in" or grant.consumed_at is not None or datetime.now(timezone.utc) >= _aware(grant.expires_at):
            return None
        if grant.client_id != client.get_client_id():
            return None
        return _Credential(grant.client_id, grant.user_id, redirect_uri=grant.callback_uri, code_challenge=grant.code_challenge)

    def delete_authorization_code(self, authorization_code):
        pass

    def authenticate_user(self, authorization_code):
        return authorization_code.user_id

    def create_token_response(self):
        """Consume the code and issue tokens in one transaction, re-checking the code under a lock."""
        form = self.request.form
        try:
            pair = exchange_handoff_code(self.request.engine, form["code"], form["code_verifier"], self.request.client.get_client_id(), self.request.payload.redirect_uri or "")
        except InvalidHubGrant:
            raise InvalidGrantError() from None
        self.request.token_pair = pair
        return 200, token_body(pair), self.TOKEN_RESPONSE_HEADER


class PkceS256(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]
    DEFAULT_CODE_CHALLENGE_METHOD = "S256"

    def get_authorization_code_challenge(self, authorization_code):
        return authorization_code.code_challenge

    def get_authorization_code_challenge_method(self, authorization_code):
        return "S256"


class RefreshGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = AUTH_METHODS
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, refresh_token):
        """Any stored refresh token, spent or not: refresh_hub_tokens must see a reused one to end its family."""
        if not refresh_token.startswith(REFRESH_PREFIX):
            return None
        with Session(self.request.engine) as session:
            token = session.get(HubRefreshToken, _digest(refresh_token))
            family = session.get(HubTokenFamily, token.family_id) if token is not None else None
        return _Credential(family.client_id, family.user_id) if family is not None else None

    def authenticate_user(self, refresh_token):
        return refresh_token.user_id

    def create_token_response(self):
        try:
            pair = refresh_hub_tokens(self.request.engine, self.request.form["refresh_token"], self.request.client.get_client_id())
        except InvalidHubGrant:
            raise InvalidGrantError() from None
        self.request.token_pair = pair
        return 200, token_body(pair), self.TOKEN_RESPONSE_HEADER


class Revocation(RevocationEndpoint):
    CLIENT_AUTH_METHODS = AUTH_METHODS

    def query_token(self, token_string, token_type_hint):
        raise NotImplementedError("authenticate_token queries with the request's database")

    def _query(self, token_string, engine):
        model = HubAccessToken if token_string.startswith(ACCESS_PREFIX) else HubRefreshToken if token_string.startswith(REFRESH_PREFIX) else None
        if model is None:
            return None
        with Session(engine) as session:
            token = session.get(model, _digest(token_string))
            family = session.get(HubTokenFamily, token.family_id) if token is not None else None
        return _Credential(family.client_id, family.user_id, raw=token_string) if family is not None else None

    def authenticate_token(self, request, client):
        """Another client's token is treated like an unknown one, so revocation never tells a client
        whether a token exists."""
        self.check_params(request, client)
        token = self._query(request.form["token"], request.engine)
        return token if token is not None and token.check_client(client) else None

    def revoke_token(self, token, request):
        revoke_hub_tokens(request.engine, token.raw, request.client.get_client_id())


class CrucibleAuthorizationServer(AuthorizationServer):
    def query_client(self, client_id):
        client = client_by_id(client_id)
        return OAuthClient(client) if client is not None else None

    def save_token(self, token, request):
        pass

    def send_signal(self, name, *args, **kwargs):
        pass

    def create_oauth2_request(self, request):
        return request

    def create_json_request(self, request):
        raise NotImplementedError

    def handle_response(self, status, body, headers):
        return status, body, dict(headers)


_server: CrucibleAuthorizationServer | None = None


def authorization_server() -> CrucibleAuthorizationServer:
    global _server
    if _server is None:
        server = CrucibleAuthorizationServer()
        server.register_grant(CodeGrant, [PkceS256(required=True)])
        server.register_grant(RefreshGrant)
        server.register_endpoint(Revocation)
        _server = server
    return _server
