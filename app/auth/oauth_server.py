"""The API's OAuth 2.0 authorization server, on Authlib's protocol classes: the authorization code
grant with PKCE, refresh with rotation, and revocation (RFC 7009). Authlib parses and checks each
request and shapes the errors; codes, token families and tokens stay in the API's own tables, and
are consumed and issued by the functions in app.auth.login_transactions and app.auth.tokens."""
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from authlib.oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import ClientMixin, InvalidGrantError, OAuth2Request
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc7009 import RevocationEndpoint
from authlib.oauth2.rfc6749 import UnauthorizedClientError
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc8628 import DEVICE_CODE_GRANT_TYPE, DeviceAuthorizationEndpoint, DeviceCodeGrant
from sqlalchemy import update
from sqlmodel import Session, select

from app.auth.clients import ClientUnavailable, RegisteredClient, client_by_id
from app.config import ConfigurationError, optional_secret
from app.tables import AccessToken, LoginCode, RefreshToken, TokenFamily
from app.auth.login_transactions import exchange_handoff_code
from app.auth.tokens import ACCESS_PREFIX, REFRESH_PREFIX, TokenPair, InvalidGrant, refresh_tokens, revoke_tokens

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

    def __init__(self, uri: str, headers: dict[str, str], fields: list[tuple[str, str]], engine, address: str | None = None):
        super().__init__("POST", uri, headers=headers)
        self.engine = engine
        self.address = address
        self.payload = _FormPayload(fields)
        self._form = self.payload.data
        self.token_pair: TokenPair | None = None

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
        its client_id and relies on PKCE (RFC 9700 2.1.1). The API's own device-approval client never
        reaches these endpoints."""
        if self.client.type == "internal":
            return False
        return method == ("none" if self.client.is_public else "client_secret_basic")

    def check_response_type(self, response_type):
        return response_type == "code"

    def check_grant_type(self, grant_type):
        if self.client.type == "internal":
            return False
        if grant_type == DEVICE_CODE_GRANT_TYPE:
            return self.client.is_public
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


def token_body(pair: TokenPair, now: datetime | None = None) -> dict:
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
            grant = session.exec(select(LoginCode).where(LoginCode.code_hash == _digest(code))).one_or_none()
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
        except InvalidGrant:
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
        """Any stored refresh token, spent or not: refresh_tokens must see a reused one to end its family."""
        if not refresh_token.startswith(REFRESH_PREFIX):
            return None
        with Session(self.request.engine) as session:
            token = session.get(RefreshToken, _digest(refresh_token))
            family = session.get(TokenFamily, token.family_id) if token is not None else None
        return _Credential(family.client_id, family.user_id) if family is not None else None

    def authenticate_user(self, refresh_token):
        return refresh_token.user_id

    def create_token_response(self):
        try:
            pair = refresh_tokens(self.request.engine, self.request.form["refresh_token"], self.request.client.get_client_id())
        except InvalidGrant:
            raise InvalidGrantError() from None
        self.request.token_pair = pair
        return 200, token_body(pair), self.TOKEN_RESPONSE_HEADER


class Revocation(RevocationEndpoint):
    CLIENT_AUTH_METHODS = AUTH_METHODS

    def query_token(self, token_string, token_type_hint):
        raise NotImplementedError("authenticate_token queries with the request's database")

    def _query(self, token_string, engine):
        model = AccessToken if token_string.startswith(ACCESS_PREFIX) else RefreshToken if token_string.startswith(REFRESH_PREFIX) else None
        if model is None:
            return None
        with Session(engine) as session:
            token = session.get(model, _digest(token_string))
            family = session.get(TokenFamily, token.family_id) if token is not None else None
        return _Credential(family.client_id, family.user_id, raw=token_string) if family is not None else None

    def authenticate_token(self, request, client):
        """Another client's token is treated like an unknown one, so revocation never tells a client
        whether a token exists."""
        self.check_params(request, client)
        token = self._query(request.form["token"], request.engine)
        return token if token is not None and token.check_client(client) else None

    def revoke_token(self, token, request):
        revoke_tokens(request.engine, token.raw, request.client.get_client_id())


class DeviceAuthorization(DeviceAuthorizationEndpoint):
    CLIENT_AUTH_METHODS = AUTH_METHODS

    def create_endpoint_response(self, request):
        """RFC 8628 3.1-3.2. Only clients allowed the device grant get codes."""
        from app.auth.devices import DEVICE_LIFETIME, POLL_INTERVAL, create_device_authorization
        from app.auth.login_transactions import public_api_url

        client = self.authenticate_client(request)
        if not client.check_grant_type(DEVICE_CODE_GRANT_TYPE):
            raise UnauthorizedClientError("This client may not use the device flow")
        device_code, user_code = create_device_authorization(request.engine, client.get_client_id(), requested_from=request.address)
        verification_uri = f"{public_api_url()}/oauth/device"
        return 200, {
            "device_code": device_code, "user_code": user_code, "verification_uri": verification_uri,
            "verification_uri_complete": f"{verification_uri}?{urlencode({'user_code': user_code})}",
            "expires_in": int(DEVICE_LIFETIME.total_seconds()), "interval": POLL_INTERVAL,
        }, [("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("Pragma", "no-cache")]


class _DeviceCredential:
    def __init__(self, row):
        self.row = row

    def get_client_id(self):
        return self.row.client_id

    def get_scope(self):
        return ""

    def get_user_code(self):
        return self.row

    def is_expired(self):
        return datetime.now(timezone.utc) >= _aware(self.row.expires_at)


class DeviceGrant(DeviceCodeGrant):
    """Polling with the device code (RFC 8628 3.4-3.5): pending, slow_down, access_denied,
    expired_token, or tokens once the person approved. The code works once."""
    TOKEN_ENDPOINT_AUTH_METHODS = AUTH_METHODS

    def query_device_credential(self, device_code):
        from app.auth.devices import device_code_hash
        from app.tables import DeviceAuthorization as Row

        with Session(self.request.engine) as session:
            row = session.get(Row, device_code_hash(device_code))
            if row is None or row.consumed_at is not None:
                return None
            session.expunge(row)
        return _DeviceCredential(row)

    def query_user_grant(self, row):
        if row.denied_at is not None:
            return row.user_id, False
        if row.approved_at is not None:
            return row.user_id, True
        return None

    def should_slow_down(self, credential):
        """Each poll before the interval has passed adds 5 seconds to it, for this and every later poll."""
        from app.auth.devices import SLOW_DOWN_STEP
        from app.tables import DeviceAuthorization as Row

        current = datetime.now(timezone.utc)
        row = credential.row
        too_soon = row.last_polled_at is not None and current - _aware(row.last_polled_at) < timedelta(seconds=row.interval)
        values = {"last_polled_at": current}
        if too_soon:
            values["interval"] = Row.interval + SLOW_DOWN_STEP
        with Session(self.request.engine) as session, session.begin():
            session.execute(update(Row).where(Row.device_code_hash == row.device_code_hash).values(**values).execution_options(synchronize_session=False))
        return too_soon

    def create_token_response(self):
        """Use up the approved device sign-in and issue the session in one transaction."""
        from app.tables import DeviceAuthorization as Row
        from app.auth.tokens import DEVICE_CODE, issue_tokens_in_session

        row = self.request.credential.row
        current = datetime.now(timezone.utc)
        with Session(self.request.engine) as session, session.begin():
            claimed = session.execute(update(Row).where(
                Row.device_code_hash == row.device_code_hash, Row.consumed_at.is_(None), Row.approved_at.is_not(None), Row.expires_at > current,
            ).values(consumed_at=current).execution_options(synchronize_session=False).returning(Row.user_id, Row.auth_time)).first()
            if claimed is None:
                raise InvalidGrantError()
            try:
                pair = issue_tokens_in_session(session, claimed[0], self.request.client.get_client_id(), now=current, auth_time=_aware(claimed[1]) if claimed[1] else None, grant_type=DEVICE_CODE)
            except InvalidGrant:
                raise InvalidGrantError() from None
        self.request.token_pair = pair
        return 200, token_body(pair), self.TOKEN_RESPONSE_HEADER


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
        server.register_grant(DeviceGrant)
        server.register_endpoint(DeviceAuthorization)
        _server = server
    return _server
