"""Asking for an account with a password. The account waits until an administrator approves it, and
its email is not verified, so nothing trusts it: no sign-in or reset goes by email."""
from dataclasses import dataclass
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException
from sqlmodel import Session

from app.models.core_models import User, get_mfid_value

IN_USE = "This username or email is already in use. Choose another username, or ask an administrator if you already have an account."


@dataclass(frozen=True)
class RegistrationProblem:
    field: str
    message: str
    status: int = 422


class RegistrationRefused(Exception):
    def __init__(self, problem: RegistrationProblem):
        super().__init__(problem.message)
        self.problem = problem


def registration_open() -> bool:
    from app.config import get_settings

    auth = get_settings().auth
    return auth.password_enabled and auth.password_registration == "approval"


def checked_fields(session: Session, *, username: str, first_name: str, last_name: str, email: str) -> tuple[str, str, str, str]:
    """The cleaned username, names and email, or RegistrationRefused naming the field. A username or
    email already held gets one shared answer, so the form does not say which one exists."""
    from app.utils.user_utils import assert_profile_fields_available, assert_self_chosen_username_allowed, validate_username_format

    first, last = first_name.strip(), last_name.strip()
    if not first or not last:
        raise RegistrationRefused(RegistrationProblem("name", "Give your first and last name, so an administrator knows who is asking."))
    try:
        cleaned_username = validate_username_format(username.strip())
    except ValueError as error:
        raise RegistrationRefused(RegistrationProblem("username", str(error).capitalize())) from None
    try:
        cleaned_email = validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as error:
        raise RegistrationRefused(RegistrationProblem("email", str(error))) from None
    try:
        assert_self_chosen_username_allowed(cleaned_username, None)
    except HTTPException:
        raise RegistrationRefused(RegistrationProblem("username", "This username is reserved; choose another.")) from None
    try:
        assert_profile_fields_available(session, username=cleaned_username, email=cleaned_email)
    except HTTPException:
        raise RegistrationRefused(RegistrationProblem("username", IN_USE, 409)) from None
    return cleaned_username, first, last, cleaned_email


def register(session: Session, *, username: str, first_name: str, last_name: str, email: str, password: str, now: datetime | None = None) -> User:
    """Create the waiting account with its password, in the caller's transaction. The fields must
    already have passed checked_fields."""
    from app.auth.events import AuthEventType, record_event
    from app.auth.passwords import store_password
    from app.utils.user_utils import provision_user

    user = provision_user(session, unique_id=get_mfid_value(), first_name=first_name, last_name=last_name, email=email, username=username)
    user.pending_at = now or datetime.now(timezone.utc)
    session.add(user)
    session.flush()
    store_password(session, user, password)
    record_event(session, AuthEventType.REGISTERED, user=user)
    return user
