import argparse
import json
import sys
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.general_utils import is_orcid
from app.utils.permissions import PlatformRole
from app.utils.user_utils import validate_username_format


class Refused(Exception):
    pass


ROLES = (PlatformRole.NONE.value, PlatformRole.CONTRIBUTOR.value, PlatformRole.ADMIN.value)


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("admin", help="Manage platform administrators").add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create",
        help="Create a platform administrator and print their API key",
        description="Asks for missing values when run in a terminal; with --no-input or without a terminal, missing required values are an error.",
    )
    create.add_argument("--first-name")
    create.add_argument("--last-name")
    create.add_argument("--email")
    create.add_argument("--orcid", help="Use this ORCID as the identifier; without it the user gets a Crucible MFID")
    create.add_argument("--username", help="Generated from the name when omitted")
    create.add_argument("--allow-additional", action="store_true", help="Create the administrator even if one already exists")
    create.add_argument("--no-input", action="store_true", help="Never prompt")
    create.set_defaults(handler=_run_create)
    set_role = commands.add_parser(
        "set-role",
        help="Change a person's platform role",
        description="For when no administrator can sign in to the Hub. Refuses to demote the last active human administrator.",
    )
    set_role.add_argument("user", help="unique_id, linked ORCID, username or email")
    set_role.add_argument("role", choices=ROLES)
    set_role.set_defaults(handler=_run_set_role)
    password_link = commands.add_parser(
        "password-link",
        help="Print a one-time link for a person to set or reset their password",
        description="Works for any person, administrators included, so a locked-out administrator can recover from the server. Replaces any earlier link.",
    )
    password_link.add_argument("user", help="unique_id, linked ORCID, username or email")
    password_link.set_defaults(handler=_run_password_link)
    pending = commands.add_parser("pending", help="List accounts waiting for approval, oldest first")
    pending.add_argument("--json", action="store_true", help="One JSON object per line instead of a table")
    pending.set_defaults(handler=_run_pending)
    approve = commands.add_parser("approve", help="Let a waiting account in")
    approve.add_argument("user", help="unique_id, username or email of the waiting account")
    approve.set_defaults(handler=_run_decide, approve=True)
    reject = commands.add_parser("reject", help="Delete a waiting account, freeing its username and login")
    reject.add_argument("user", help="unique_id, username or email of the waiting account")
    reject.set_defaults(handler=_run_decide, approve=False)


def _name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


def _email(value: str) -> Optional[str]:
    return value.strip().lower() or None


def _orcid(value: str) -> Optional[str]:
    value = value.strip()
    if value and not is_orcid(value):
        raise ValueError("must be an ORCID such as 0000-0002-1825-0097")
    return value or None


def _username(value: str) -> Optional[str]:
    value = value.strip()
    return validate_username_format(value) if value else None


FIELDS: list[tuple[str, str, Callable[[str], Optional[str]], bool]] = [
    ("first_name", "First name", _name, True),
    ("last_name", "Last name", _name, True),
    ("email", "Email (optional)", _email, False),
    ("orcid", "ORCID (optional; leave empty to assign a Crucible MFID)", _orcid, False),
    ("username", "Username (optional; leave empty to generate one)", _username, False),
]


def _collect(args: argparse.Namespace, interactive: bool) -> dict:
    values = {}
    for name, prompt, clean, required in FIELDS:
        given = getattr(args, name)
        flag = "--" + name.replace("_", "-")
        if given is not None:
            try:
                values[name] = clean(given)
            except ValueError as error:
                raise Refused(f"{flag} {error}") from None
        elif interactive:
            while True:
                try:
                    values[name] = clean(input(f"{prompt}: "))
                    break
                except ValueError as error:
                    print(f"{prompt} {error}", file=sys.stderr)
        elif required:
            raise Refused(f"{flag} is required with --no-input or without a terminal")
        else:
            values[name] = None
    return values


def _detail(error: HTTPException) -> str:
    detail = error.detail
    if isinstance(detail, dict):
        return detail.get("message") or ", ".join(detail.get("fields", [])) or str(detail)
    return str(detail)


def _create(values: dict, allow_additional: bool) -> tuple[str, str, str]:
    """Create the administrator and their API key in one transaction."""
    from app.api.dependencies import get_engine
    from app.auth.identity import IdentityTaken
    from app.models.core_models import AccessGroup, User, get_mfid_value
    from app.utils.auth_utils import add_apikey
    from app.utils.user_utils import assert_profile_fields_available, commit_user, generate_unique_username, provision_user

    unique_id = values["orcid"] or get_mfid_value()
    with Session(get_engine()) as session:
        existing = session.exec(select(User).where(User.platform_role == PlatformRole.ADMIN.value, User.is_service_account.is_(False))).all()
        if existing and not allow_additional:
            names = ", ".join(user.username or user.unique_id for user in existing)
            raise Refused(f"A platform administrator already exists ({names}); pass --allow-additional to create another")
        if session.exec(select(User).where(User.unique_id == unique_id)).one_or_none() is not None:
            raise Refused(f"A user with identifier {unique_id} already exists")
        if session.exec(select(AccessGroup).where(AccessGroup.group_name == unique_id)).one_or_none() is not None:
            raise Refused(f"An access group named {unique_id} already exists")
        try:
            assert_profile_fields_available(session, username=values["username"], email=values["email"])
        except HTTPException as error:
            raise Refused(f"Already in use: {_detail(error)}") from None
        username = values["username"] or generate_unique_username(session, values["first_name"], values["last_name"])
        try:
            user = provision_user(
                session, unique_id=unique_id, first_name=values["first_name"], last_name=values["last_name"],
                email=values["email"], username=username, platform_role=PlatformRole.ADMIN,
            )
            api_key = add_apikey(user, session)
            commit_user(session)
        except IdentityTaken:
            raise Refused(f"ORCID {unique_id} is already linked to another user; nothing created") from None
        except HTTPException as error:
            raise Refused(f"Already in use: {_detail(error)}") from None
        except IntegrityError:
            raise Refused("Another user with the same identifier, username, or email was created at the same time; nothing created") from None
    return unique_id, username, api_key


def _summary(values: dict) -> str:
    username = values["username"] or "generated from the name"
    identifier = values["orcid"] or "a new Crucible MFID"
    email = values["email"] or "none"
    return f"{values['first_name']} {values['last_name']}, username {username}, identifier {identifier}, email {email}"


def _ask(question: str) -> bool:
    return input(f"{question} [y/N]: ").strip().lower() in {"y", "yes"}


def _has_terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except ValueError:
        return False


def _run_create(args: argparse.Namespace) -> int:
    interactive = not args.no_input and _has_terminal()
    try:
        values = _collect(args, interactive)
        if interactive and not _ask(f"Create platform administrator {_summary(values)}?"):
            raise Refused("Nothing created")
        unique_id, username, api_key = _create(values, args.allow_additional)
    except Refused as error:
        print(error, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; nothing created", file=sys.stderr)
        return 1
    created = {"unique_id": unique_id, "username": username, "api_key": api_key}
    print(f"Created platform administrator {username}. The API key below signs in as this administrator; keep it secret.", file=sys.stderr)
    from app.config import get_settings

    if get_settings().auth.password_enabled:
        try:
            _, _, expires_at, url = _password_link(unique_id)
        except Refused as error:
            print(f"No password link: {error}", file=sys.stderr)
        else:
            created.update(password_link=url, password_link_expires_at=expires_at)
            print("Open password_link in a browser to set this administrator's password; it works once.", file=sys.stderr)
    print(json.dumps(created))
    return 0


def _set_role(user_ref: str, role: str) -> tuple[str, str]:
    from app.api.dependencies import get_engine
    from app.auth.events import AuthEventType, record_event
    from app.models.core_models import User
    from app.utils.auth_utils import forget_cached_keys
    from app.utils.user_utils import person_by_reference

    with Session(get_engine()) as session:
        user = person_by_reference(session, user_ref)
        if user is None:
            raise Refused(f"No user matches {user_ref}")
        if user.is_service_account:
            raise Refused(f"{user.unique_id} is a service account; change its role through the service account routes")
        previous = user.platform_role
        if previous == role:
            return user.username or user.unique_id, previous
        if previous == PlatformRole.ADMIN.value:
            active_admins = session.exec(
                select(User.id).where(User.platform_role == PlatformRole.ADMIN.value, User.is_service_account == False, User.disabled_at == None).order_by(User.id).with_for_update()  # noqa: E711, E712
            ).all()
            if not [admin_id for admin_id in active_admins if admin_id != user.id]:
                raise Refused("Cannot demote the last active human platform administrator; nothing changed")
        user.platform_role = role
        session.add(user)
        record_event(session, AuthEventType.ROLE_CHANGED, user=user)
        session.commit()
        forget_cached_keys(session, user.id)
        return user.username or user.unique_id, previous


def _run_set_role(args: argparse.Namespace) -> int:
    try:
        name, previous = _set_role(args.user, args.role)
    except Refused as error:
        print(error, file=sys.stderr)
        return 1
    if previous == args.role:
        print(f"{name} is already {args.role}; nothing changed", file=sys.stderr)
    else:
        print(f"Changed {name} from {previous} to {args.role}", file=sys.stderr)
    return 0



def _password_link(user_ref: str) -> tuple[str, str, str, str]:
    from app.api.dependencies import get_engine
    from app.api.routes.passwords import link_url
    from app.auth.password_links import LinkRefused, issue_link, link_purpose
    from app.config import get_settings
    from app.utils.hub_login import HubLoginUnavailable, public_api_url
    from app.utils.user_utils import person_by_reference

    if not get_settings().auth.password_enabled:
        raise Refused("Password sign-in is off; set CRUCIBLE_AUTH_PASSWORD_ENABLED=true first")
    try:
        base = public_api_url()
    except HubLoginUnavailable:
        raise Refused("The public API address is not set, so no link can be made") from None
    with Session(get_engine()) as session, session.begin():
        user = person_by_reference(session, user_ref)
        if user is None:
            raise Refused(f"No user matches {user_ref}")
        purpose = link_purpose(session, user)
        try:
            token, expires_at = issue_link(session, user, purpose, actor=None)
        except LinkRefused as refused:
            raise Refused(refused.message) from None
        return user.username or user.unique_id, purpose, expires_at.isoformat(timespec="minutes"), link_url(base, token)


def _run_password_link(args: argparse.Namespace) -> int:
    try:
        name, purpose, expires_at, url = _password_link(args.user)
    except Refused as error:
        print(error, file=sys.stderr)
        return 1
    print(f"A link to {'set' if purpose == 'setup' else 'reset'} the password of {name}, valid once until {expires_at}. Pass it on privately:", file=sys.stderr)
    print(url)
    return 0


def _pending_accounts() -> list[dict]:
    from app.api.dependencies import get_engine
    from app.auth.linking import sign_in_labels
    from app.models.core_models import User

    with Session(get_engine()) as session:
        users = session.exec(select(User).where(User.pending_at != None).order_by(User.pending_at, User.id)).all()  # noqa: E711
        labels = sign_in_labels(session, [user.id for user in users])
        return [{
            "unique_id": user.unique_id, "username": user.username, "name": " ".join(part for part in (user.first_name, user.last_name) if part),
            "email": user.email, "asked_at": user.pending_at.isoformat(timespec="minutes"), "sign_in_methods": labels[user.id],
        } for user in users]


def _run_pending(args: argparse.Namespace) -> int:
    accounts = _pending_accounts()
    if args.json:
        for account in accounts:
            print(json.dumps(account))
        return 0
    if not accounts:
        print("No accounts are waiting for approval", file=sys.stderr)
        return 0
    for account in accounts:
        print(f"{account['asked_at']}  {account['username'] or '-'}  {account['name']}  {account['email'] or '-'}  {', '.join(account['sign_in_methods']) or '-'}  {account['unique_id']}")
    return 0


def _decide(user_ref: str, approve: bool) -> str:
    from app.api.dependencies import get_engine
    from app.auth.registration import NotPending, StillReferenced, approve_pending_user, reject_pending_user
    from app.utils.user_utils import person_by_reference

    with Session(get_engine()) as session:
        user = person_by_reference(session, user_ref, include_pending=True)
        if user is None:
            raise Refused(f"No account matches {user_ref}")
        name = user.username or user.unique_id
        try:
            if approve:
                approve_pending_user(session, user, actor=None)
            else:
                reject_pending_user(session, user, actor=None)
        except NotPending:
            raise Refused(f"{name} is not waiting for approval; nothing changed") from None
        except StillReferenced as referenced:
            raise Refused(f"{name} cannot be rejected: {'; '.join(referenced.reasons)}") from None
        session.commit()
        return name


def _run_decide(args: argparse.Namespace) -> int:
    try:
        name = _decide(args.user, args.approve)
    except Refused as error:
        print(error, file=sys.stderr)
        return 1
    print(f"{'Approved' if args.approve else 'Rejected and deleted'} {name}", file=sys.stderr)
    return 0
