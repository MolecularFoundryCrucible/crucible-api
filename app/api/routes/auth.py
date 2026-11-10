from fastapi import APIRouter

from app.api.removed import REMOVED_RESPONSE, removed
from app.config import get_settings
from app.models.response_schemas import AuthMethodRead

# Note: /account/* routes live in account.py

router = APIRouter(tags=["auth"])

BROWSER_KEY_FLOW_REMOVED = (
    "Getting an API key from this browser page was removed. Sign in and create a named key with POST /account/apikeys "
    "(the Hub's Security page does this), or sign in from nano-crucible 4.0 or later; programs sign people in through /oauth/authorize."
)


@router.get("/auth/methods", response_model=list[AuthMethodRead])
def list_sign_in_methods():
    """The ways to sign in on this deployment, each a `provider` value for the sign-in routes; empty
    when sign-in is off. With passwords on, the last entry is `password`, which carries
    `registration_url` while people can ask for an account."""
    from app.auth.clients import sign_in_enabled
    from app.auth.password_registration import registration_open
    from app.utils.hub_login import LOCAL_PROVIDER, HubLoginUnavailable, public_api_url

    settings = get_settings()
    if not sign_in_enabled():
        return []
    methods = [AuthMethodRead(key=provider.key, display_name=provider.display_name, is_orcid=provider.is_orcid) for provider in settings.auth.providers]
    if settings.auth.password_enabled:
        registration_url = None
        if registration_open():
            try:
                registration_url = f"{public_api_url()}/oauth/register"
            except HubLoginUnavailable:
                registration_url = None
        methods.append(AuthMethodRead(key=LOCAL_PROVIDER, display_name="Password", kind="password", is_orcid=False, registration_url=registration_url))
    return methods


_GONE = {"deprecated": True, "status_code": 410, "responses": REMOVED_RESPONSE}


@router.get("/auth/login", **_GONE)
@router.get("/auth/callback", **_GONE)
@router.get("/auth/apikey", **_GONE)
@router.post("/auth/apikey/regenerate", **_GONE)
@router.get("/login", **_GONE)
@router.get("/auth", **_GONE)
@router.get("/user_apikey", **_GONE)
def removed_browser_key_flow():
    """Removed: the browser pages that signed in with ORCID and handed out a single API key."""
    removed(BROWSER_KEY_FLOW_REMOVED)
