from fastapi import APIRouter

from app.config import get_settings
from app.models.response_schemas import AuthMethodRead

# Note: /account/* routes live in account.py

router = APIRouter(tags=["auth"])

@router.get("/auth/methods", response_model=list[AuthMethodRead])
def list_sign_in_methods():
    """The ways to sign in on this deployment, each a `provider` value for the sign-in routes; empty
    when sign-in is off. With passwords on, the last entry is `password`, which carries
    `registration_url` while people can ask for an account."""
    from app.auth.clients import sign_in_enabled
    from app.auth.password_registration import registration_open
    from app.auth.login_transactions import LOCAL_PROVIDER, SignInUnavailable, public_api_url

    settings = get_settings()
    if not sign_in_enabled():
        return []
    methods = [AuthMethodRead(key=provider.key, display_name=provider.display_name, is_orcid=provider.is_orcid) for provider in settings.auth.providers]
    if settings.auth.password_enabled:
        registration_url = None
        if registration_open():
            try:
                registration_url = f"{public_api_url()}/oauth/register"
            except SignInUnavailable:
                registration_url = None
        methods.append(AuthMethodRead(key=LOCAL_PROVIDER, display_name="Password", kind="password", is_orcid=False, registration_url=registration_url))
    return methods
