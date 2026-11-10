"""Self-or-platform-admin checks on user-scoped routes."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.utils.auth_utils import require_self_or_platform_admin
from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role

ME, SOMEONE_ELSE = SimpleNamespace(id=1, unique_id="0000-0001"), SimpleNamespace(id=2, unique_id="0000-0002")


def principal(user=ME, *, platform_role=PlatformRole.NONE, groups=()):
    return Principal(user_id=user.unique_id, internal_user_id=user.id, platform_role=platform_role, groups=tuple(groups))


def test_self_is_allowed():
    require_self_or_platform_admin(principal(ME), ME)


def test_platform_admin_may_act_on_anyone():
    require_self_or_platform_admin(principal(ME, platform_role=PlatformRole.ADMIN), SOMEONE_ELSE)


def test_platform_admin_passes_for_a_missing_user_so_the_route_can_404():
    require_self_or_platform_admin(principal(ME, platform_role=PlatformRole.ADMIN), None)


def test_missing_user_is_403_for_anyone_else():
    with pytest.raises(HTTPException) as exc:
        require_self_or_platform_admin(principal(ME), None)
    assert exc.value.status_code == 403


def test_admin_group_membership_does_not_grant_platform_authority():
    with pytest.raises(HTTPException) as exc:
        require_self_or_platform_admin(principal(ME, groups=[GroupMembership(1, "admin", Role.VIEWER)]), SOMEONE_ELSE)
    assert exc.value.status_code == 403


def test_someone_else_is_denied():
    with pytest.raises(HTTPException) as exc:
        require_self_or_platform_admin(principal(ME), SOMEONE_ELSE)
    assert exc.value.status_code == 403


def test_membership_of_anothers_orcid_group_no_longer_grants_access():
    """Nothing enforces ORCID groups having one member, so being added to someone's group must not
    let you read their data and PATCH their profile."""
    impostor = principal(ME, groups=[GroupMembership(7, SOMEONE_ELSE.unique_id, Role.OWNER)])
    with pytest.raises(HTTPException) as exc:
        require_self_or_platform_admin(impostor, SOMEONE_ELSE)
    assert exc.value.status_code == 403


def test_same_unique_id_with_another_internal_key_is_not_self():
    """After an ID rewrite a cached caller can hold an old unique_id; only the internal key counts."""
    lookalike = SimpleNamespace(id=99, unique_id=ME.unique_id)
    with pytest.raises(HTTPException):
        require_self_or_platform_admin(principal(ME), lookalike)


def test_caller_without_an_internal_key_is_never_self():
    with pytest.raises(HTTPException):
        require_self_or_platform_admin(Principal(user_id=ME.unique_id), ME)


def test_service_account_matching_itself_is_allowed():
    sa = SimpleNamespace(id=5, unique_id="0tkn2knjast3h0008nyq9zps2c")
    require_self_or_platform_admin(Principal(user_id=sa.unique_id, internal_user_id=sa.id, is_service_account=True), sa)


def test_identity_group_is_found_through_membership_not_its_name(db_engine):
    from sqlmodel import Session

    from app.tables import AccessGroup
    from app.utils.user_utils import identity_group_of, identity_group_owner, provision_user

    with Session(db_engine) as session:
        user = provision_user(session, unique_id="0000-0002-1825-0097", first_name="Ada", last_name="L", email=None, username="ada")
        session.commit()
        group = identity_group_of(session, user.id)
        group.group_name = "renamed-label"
        session.add(group)
        session.add(AccessGroup(group_name="0000-0002-1825-0097", group_type="identity"))
        session.commit()
        assert identity_group_of(session, user.id).id == group.id
        assert identity_group_owner(session, group.id).id == user.id
