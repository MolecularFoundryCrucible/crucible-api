import datetime
import logging
import secrets
import time
import urllib.parse

import httpx
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from fastapi import Request
from joserfc.errors import JoseError
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from app.api.dependencies import get_api_root_path, get_redirect_uri
from app.auth.providers import ProviderUnavailable, audience_is_only, orcid_browser_app, orcid_browser_client, orcid_id_token_claims
from app.config import ConfigurationError
from app.utils.auth_utils import encode_token, get_secret_key

logger = logging.getLogger(__name__)

LOGIN_SESSION_COOKIE = "crucible_login_session"
LOGIN_SESSION_LIFETIME = 600
PENDING_SIGN_IN = "pending_sign_in"
ID_TOKEN_LEEWAY = 30
USER_TOKEN_LIFETIME = datetime.timedelta(minutes=1)
_FAILURES = (ProviderUnavailable, ConfigurationError, OAuthError, JoseError, httpx.HTTPError, TypeError, ValueError, KeyError)


class SignInRejected(Exception):
    pass


class SignInFailed(Exception):
    pass


def login_session_middleware(app) -> SessionMiddleware:
    return SessionMiddleware(
        app,
        secret_key=get_secret_key(),
        session_cookie=LOGIN_SESSION_COOKIE,
        max_age=LOGIN_SESSION_LIFETIME,
        path=get_api_root_path() or "/",
        same_site="lax",
        https_only=True,
    )


def _pending_sign_in(request: Request) -> dict:
    """Reuse this browser's unexpired state and nonce, because Authlib keeps only the newest sign-in per browser and a page opened twice would otherwise invalidate the redirect the user is following."""
    pending = request.session.get(PENDING_SIGN_IN)
    if not pending or pending["expires"] <= time.time():
        pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32), "expires": time.time() + LOGIN_SESSION_LIFETIME}
        request.session[PENDING_SIGN_IN] = pending
    return pending


async def _abandon_sign_in(request: Request) -> None:
    """End this browser's sign-in when the callback names its state, so the state is single use. Another state leaves it running, because any site can send a browser to this callback."""
    pending = request.session.get(PENDING_SIGN_IN)
    if not pending or request.query_params.get("state") != pending["state"]:
        return
    request.session.pop(PENDING_SIGN_IN)
    await orcid_browser_app().framework.clear_state_data(request.session, pending["state"])


async def start_sign_in(request: Request) -> Response:
    try:
        client = await orcid_browser_client()
        pending = _pending_sign_in(request)
        return await client.authorize_redirect(
            request, urllib.parse.unquote_plus(get_redirect_uri()), state=pending["state"], nonce=pending["nonce"],
        )
    except _FAILURES as err:
        logger.warning("ORCID sign-in failed to start: %s", type(err).__name__)
        raise SignInFailed from None


async def finish_sign_in(request: Request) -> str:
    """Return the signed token for the `crucible_user_token` cookie. Raises SignInRejected when
    this browser did not start the sign-in or ORCID reported an error, and SignInFailed when
    ORCID cannot be reached or its answer does not verify."""
    if request.query_params.get("error") or not request.query_params.get("code"):
        logger.warning("ORCID sign-in rejected: provider error or no code")
        await _abandon_sign_in(request)
        raise SignInRejected
    try:
        client = await orcid_browser_client()
        token = await client.authorize_access_token(request, claims_options=orcid_id_token_claims(), leeway=ID_TOKEN_LEEWAY)
    except MismatchingStateError:
        logger.warning("ORCID sign-in rejected: state does not match this browser")
        raise SignInRejected from None
    except _FAILURES as err:
        request.session.pop(PENDING_SIGN_IN, None)
        logger.warning("ORCID sign-in failed: %s", type(err).__name__)
        raise SignInFailed from None
    request.session.pop(PENDING_SIGN_IN, None)
    if "id_token" not in token:
        logger.warning("ORCID sign-in failed: token response has no ID token")
        raise SignInFailed
    identity = token.get("userinfo") or {}
    if not audience_is_only(identity.get("aud"), client.client_id):
        logger.warning("ORCID sign-in failed: ID token lists another audience")
        raise SignInFailed
    subject = identity.get("sub")
    if not isinstance(subject, str) or not subject:
        logger.warning("ORCID sign-in failed: ID token has no subject")
        raise SignInFailed
    claims = {"sub": subject, "orcid": subject}
    for name in ("given_name", "family_name"):
        if identity.get(name) is not None:
            claims[name] = identity[name]
    claims["exp"] = int((datetime.datetime.now(datetime.timezone.utc) + USER_TOKEN_LIFETIME).timestamp())
    return encode_token(claims)
