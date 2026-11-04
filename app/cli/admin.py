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
            provision_user(
                session, unique_id=unique_id, first_name=values["first_name"], last_name=values["last_name"],
                email=values["email"], username=username, platform_role=PlatformRole.ADMIN,
            )
            api_key = add_apikey(unique_id, session)
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
    print(f"Created platform administrator {username}. The API key below signs in as this administrator; keep it secret.", file=sys.stderr)
    print(json.dumps({"unique_id": unique_id, "username": username, "api_key": api_key}))
    return 0
