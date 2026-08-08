"""Self-or-admin check on /users/{orcid}/*.

Behaviour is unchanged from require_groups(ag_list, {'admin', orcid}) so no
client breaks. What changes is how identity is established: directly from
principal.user_id, rather than inferred from membership of the group named
after that ORCID — an inference nothing enforces.
"""
import pytest
from fastapi import HTTPException

from app.utils.auth_utils import require_self_or_platform_admin
from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role

ME, SOMEONE_ELSE = "0000-0001", "0000-0002"


def principal(user_id=ME, *, platform_role=PlatformRole.NONE, groups=()):
    return Principal(user_id=user_id, platform_role=platform_role, groups=tuple(groups))


def test_self_is_allowed():
    require_self_or_platform_admin(principal(ME), ME)


def test_platform_admin_may_act_on_anyone():
    require_self_or_platform_admin(
        principal(ME, platform_role=PlatformRole.ADMIN), SOMEONE_ELSE)


def test_admin_group_still_works_during_the_transition():
    require_self_or_platform_admin(
        principal(ME, groups=[GroupMembership(1, "admin", Role.VIEWER)]), SOMEONE_ELSE)


def test_someone_else_is_denied():
    with pytest.raises(HTTPException) as exc:
        require_self_or_platform_admin(principal(ME), SOMEONE_ELSE)
    assert exc.value.status_code == 403


def test_membership_of_anothers_orcid_group_no_longer_grants_access():
    """The hole this closes. Nothing enforces ORCID groups having one member,
    so under the old check being added to someone's group let you read their
    data and PATCH their profile."""
    impostor = principal(ME, groups=[GroupMembership(7, SOMEONE_ELSE, Role.OWNER)])
    with pytest.raises(HTTPException) as exc:
        require_self_or_platform_admin(impostor, SOMEONE_ELSE)
    assert exc.value.status_code == 403


def test_service_account_matching_its_own_id_is_allowed():
    """A service account's user_id is an mfid, not an ORCID; the comparison is
    the same either way."""
    sa = Principal(user_id="0tkn2knjast3h0008nyq9zps2c", is_service_account=True)
    require_self_or_platform_admin(sa, "0tkn2knjast3h0008nyq9zps2c")
