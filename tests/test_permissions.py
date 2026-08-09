"""Exhaustive matrix over the pure permission model.

No database, no HTTP, no fixtures — everything here builds a Principal
directly, which is the payoff of keeping effective_role pure. Roughly 300
assertions run in milliseconds.

Design reference: docs/permission_system_plan.md
"""
import itertools

import pytest

from app.utils.permissions import (
    PUBLIC_GROUP_NAME,
    GroupMembership,
    Permission,
    PlatformRole,
    Principal,
    Role,
    THRESHOLDS,
    can_grant,
    effective_role,
    has_permission,
)

ALL_ROLES = list(Role)
ALL_PERMISSIONS = list(Permission)

PROJECT_GID = 1
ORCID_GID = 2
INSTRUMENT_GID = 3
PUBLIC_GID = 99


def principal(*memberships, platform_role=PlatformRole.NONE, is_sa=False, user_id="0000-0001"):
    return Principal(
        user_id=user_id,
        platform_role=platform_role,
        is_service_account=is_sa,
        groups=tuple(memberships),
    )


def member(gid, role, name=None):
    return GroupMembership(id=gid, group_name=name or f"group_{gid}", role=role)


class TestRoleOrdering:
    def test_declared_ascending(self):
        assert ALL_ROLES == sorted(ALL_ROLES)
        assert Role.VIEWER < Role.CONTRIBUTOR < Role.EDITOR < Role.ADMIN < Role.OWNER

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_db_value_roundtrips(self, role):
        assert Role.from_db(role.db_value) is role

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_from_db_accepts_a_role(self, role):
        assert Role.from_db(role) is role

    def test_db_values_match_the_postgres_enum(self):
        assert [r.db_value for r in ALL_ROLES] == [
            "viewer", "contributor", "editor", "admin", "owner"
        ]


class TestEffectiveRoleIsMinOfStandingAndGrant:
    @pytest.mark.parametrize("standing,grant", list(itertools.product(ALL_ROLES, ALL_ROLES)))
    def test_every_pair_resolves_to_the_weaker(self, standing, grant):
        p = principal(member(PROJECT_GID, standing))
        assert effective_role(p, [(PROJECT_GID, grant)]) == min(standing, grant)

    @pytest.mark.parametrize("grant", ALL_ROLES)
    def test_grant_caps_a_higher_standing(self, grant):
        p = principal(member(ORCID_GID, Role.OWNER))
        assert effective_role(p, [(ORCID_GID, grant)]) == grant

    @pytest.mark.parametrize("standing", ALL_ROLES)
    def test_standing_caps_a_higher_grant(self, standing):
        p = principal(member(PROJECT_GID, standing))
        assert effective_role(p, [(PROJECT_GID, Role.OWNER)]) == standing


class TestEffectiveRoleIsMaxAcrossGroups:
    def test_owner_path_wins_over_weaker_project_standing(self):
        """A contributor in P owns what they create: the ORCID path resolves to
        owner while the project path resolves to contributor."""
        p = principal(
            member(ORCID_GID, Role.OWNER),
            member(PROJECT_GID, Role.CONTRIBUTOR),
        )
        acl = [(ORCID_GID, Role.OWNER), (PROJECT_GID, Role.EDITOR)]
        assert effective_role(p, acl) == Role.OWNER

    def test_ownership_survives_leaving_the_project(self):
        p = principal(member(ORCID_GID, Role.OWNER))
        acl = [(ORCID_GID, Role.OWNER), (PROJECT_GID, Role.EDITOR)]
        assert effective_role(p, acl) == Role.OWNER

    @pytest.mark.parametrize("a,b", list(itertools.product(ALL_ROLES, ALL_ROLES)))
    def test_two_paths_take_the_stronger(self, a, b):
        p = principal(member(PROJECT_GID, Role.OWNER), member(INSTRUMENT_GID, Role.OWNER))
        acl = [(PROJECT_GID, a), (INSTRUMENT_GID, b)]
        assert effective_role(p, acl) == max(a, b)

    def test_adding_a_grant_never_reduces_access(self):
        """The no-deny invariant: the model is monotonic."""
        p = principal(member(PROJECT_GID, Role.ADMIN), member(INSTRUMENT_GID, Role.ADMIN))
        before = effective_role(p, [(PROJECT_GID, Role.ADMIN)])
        after = effective_role(p, [(PROJECT_GID, Role.ADMIN), (INSTRUMENT_GID, Role.VIEWER)])
        assert after >= before


class TestNoAccess:
    def test_no_acl_rows_at_all(self):
        p = principal(member(PROJECT_GID, Role.OWNER))
        assert effective_role(p, []) is None

    def test_acl_names_a_group_the_principal_is_not_in(self):
        p = principal(member(PROJECT_GID, Role.OWNER))
        assert effective_role(p, [(INSTRUMENT_GID, Role.ADMIN)]) is None

    def test_principal_with_no_groups(self):
        assert effective_role(principal(), [(PROJECT_GID, Role.OWNER)]) is None


class TestInputRobustness:
    def test_duplicate_group_rows_take_the_strongest_not_the_last(self):
        """A join that fans out must not under-grant."""
        p = principal(member(PROJECT_GID, Role.OWNER))
        assert effective_role(p, [(PROJECT_GID, Role.OWNER), (PROJECT_GID, Role.VIEWER)]) == Role.OWNER
        assert effective_role(p, [(PROJECT_GID, Role.VIEWER), (PROJECT_GID, Role.OWNER)]) == Role.OWNER

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_grants_may_arrive_as_database_strings(self, role):
        p = principal(member(PROJECT_GID, Role.OWNER))
        assert effective_role(p, [(PROJECT_GID, role.db_value)]) == role

    def test_thresholds_cannot_be_mutated(self):
        with pytest.raises(TypeError):
            THRESHOLDS[Permission.DELETE] = Role.VIEWER

    def test_permission_argument_is_required(self):
        """No permissive default: omitting it must fail loudly rather than
        silently performing the weakest check."""
        p = principal(member(PROJECT_GID, Role.VIEWER))
        with pytest.raises(TypeError):
            has_permission(p, [(PROJECT_GID, Role.OWNER)])

    @pytest.mark.parametrize("permission", ALL_PERMISSIONS)
    def test_no_access_denies_every_permission(self, permission):
        p = principal(member(PROJECT_GID, Role.OWNER))
        assert has_permission(p, [], permission) is False


class TestThresholds:
    @pytest.mark.parametrize(
        "role,permission", list(itertools.product(ALL_ROLES, ALL_PERMISSIONS))
    )
    def test_permission_granted_exactly_at_or_above_its_threshold(self, role, permission):
        p = principal(member(PROJECT_GID, role))
        acl = [(PROJECT_GID, Role.OWNER)]
        assert has_permission(p, acl, permission) == (role >= THRESHOLDS[permission])

    def test_viewer_reads_but_cannot_create(self):
        p = principal(member(PROJECT_GID, Role.VIEWER))
        acl = [(PROJECT_GID, Role.OWNER)]
        assert has_permission(p, acl, Permission.READ)
        assert not has_permission(p, acl, Permission.CREATE)

    def test_contributor_creates_but_cannot_edit_others(self):
        p = principal(member(PROJECT_GID, Role.CONTRIBUTOR))
        acl = [(PROJECT_GID, Role.OWNER)]
        assert has_permission(p, acl, Permission.CREATE)
        assert not has_permission(p, acl, Permission.EDIT)

    def test_editor_edits_but_cannot_delete(self):
        p = principal(member(PROJECT_GID, Role.EDITOR))
        acl = [(PROJECT_GID, Role.OWNER)]
        assert has_permission(p, acl, Permission.EDIT)
        assert not has_permission(p, acl, Permission.DELETE)

    def test_admin_deletes_but_cannot_transfer(self):
        p = principal(member(PROJECT_GID, Role.ADMIN))
        acl = [(PROJECT_GID, Role.OWNER)]
        assert has_permission(p, acl, Permission.DELETE)
        assert not has_permission(p, acl, Permission.TRANSFER)

    def test_owner_transfers(self):
        p = principal(member(PROJECT_GID, Role.OWNER))
        assert has_permission(p, [(PROJECT_GID, Role.OWNER)], Permission.TRANSFER)


class TestContributorOwnVsPeer:
    """The load-bearing consequence, per the plan: contributors managing their
    own resources is not a rule, it falls out of MAX. Any is_owner shortcut
    added later would diverge from this."""

    OWN = [(ORCID_GID, Role.OWNER), (PROJECT_GID, Role.EDITOR)]
    PEER = [(999, Role.OWNER), (PROJECT_GID, Role.EDITOR)]

    def _contributor(self):
        return principal(
            member(ORCID_GID, Role.OWNER),
            member(PROJECT_GID, Role.CONTRIBUTOR),
        )

    def test_can_edit_own(self):
        assert has_permission(self._contributor(), self.OWN, Permission.EDIT)

    def test_cannot_edit_a_peers(self):
        assert not has_permission(self._contributor(), self.PEER, Permission.EDIT)

    def test_can_delete_own(self):
        assert has_permission(self._contributor(), self.OWN, Permission.DELETE)

    def test_cannot_delete_a_peers(self):
        assert not has_permission(self._contributor(), self.PEER, Permission.DELETE)

    def test_can_still_read_a_peers(self):
        assert has_permission(self._contributor(), self.PEER, Permission.READ)

    def test_still_owns_own_after_leaving_the_project(self):
        departed = principal(member(ORCID_GID, Role.OWNER))
        assert has_permission(departed, self.OWN, Permission.TRANSFER)


class TestSharing:
    def test_share_as_viewer_does_not_grant_owner(self):
        """Everyone holds owner in their own ORCID group; without MIN this
        would hand the recipient delete."""
        alice = principal(member(ORCID_GID, Role.OWNER))
        assert effective_role(alice, [(ORCID_GID, Role.VIEWER)]) == Role.VIEWER
        assert not has_permission(alice, [(ORCID_GID, Role.VIEWER)], Permission.DELETE)

    @pytest.mark.parametrize("shared_at", ALL_ROLES)
    def test_recipient_never_exceeds_the_share(self, shared_at):
        alice = principal(member(ORCID_GID, Role.OWNER))
        assert effective_role(alice, [(ORCID_GID, shared_at)]) == shared_at


class TestPublicGroup:
    def test_public_grants_viewer_and_nothing_more(self):
        anyone = principal(member(PUBLIC_GID, Role.VIEWER, PUBLIC_GROUP_NAME))
        acl = [(PUBLIC_GID, Role.VIEWER)]
        assert has_permission(anyone, acl, Permission.READ)
        for permission in ALL_PERMISSIONS:
            if permission is not Permission.READ:
                assert not has_permission(anyone, acl, permission)

    @pytest.mark.parametrize("grant", ALL_ROLES)
    def test_world_writable_is_structurally_impossible(self, grant):
        """Standing in the public group is always viewer, so min() caps any
        grant at viewer no matter how the ACL row was written."""
        anyone = principal(member(PUBLIC_GID, Role.VIEWER, PUBLIC_GROUP_NAME))
        assert effective_role(anyone, [(PUBLIC_GID, grant)]) == Role.VIEWER

    def test_a_member_keeps_their_stronger_path_on_a_public_resource(self):
        p = principal(
            member(PUBLIC_GID, Role.VIEWER, PUBLIC_GROUP_NAME),
            member(PROJECT_GID, Role.ADMIN),
        )
        acl = [(PUBLIC_GID, Role.VIEWER), (PROJECT_GID, Role.ADMIN)]
        assert effective_role(p, acl) == Role.ADMIN

    def test_excluded_from_real_group_names(self):
        p = principal(
            member(PUBLIC_GID, Role.VIEWER, PUBLIC_GROUP_NAME),
            member(PROJECT_GID, Role.ADMIN, "proj_x"),
        )
        assert p.real_group_names() == ["proj_x"]


class TestPlatformAdmin:
    @pytest.mark.parametrize("permission", ALL_PERMISSIONS)
    def test_bypasses_every_permission_with_no_acl_rows(self, permission):
        admin = principal(platform_role=PlatformRole.ADMIN)
        assert has_permission(admin, [], permission) is True

    def test_bypass_is_not_an_effective_role(self):
        """Bypass is never materialised; effective_role stays honest so shadow
        mode compares like with like."""
        admin = principal(platform_role=PlatformRole.ADMIN)
        assert effective_role(admin, []) is None

    @pytest.mark.parametrize("permission", ALL_PERMISSIONS)
    def test_support_tier_gets_no_bypass_yet(self, permission):
        support = principal(platform_role=PlatformRole.SUPPORT)
        assert has_permission(support, [], permission) is False

    def test_service_account_flag_grants_nothing(self):
        sa = principal(member(INSTRUMENT_GID, Role.ADMIN), is_sa=True)
        assert not has_permission(sa, [], Permission.READ)
        assert not has_permission(sa, [(PROJECT_GID, Role.OWNER)], Permission.READ)

    def test_is_platform_admin_only_true_for_admin(self):
        assert principal(platform_role=PlatformRole.ADMIN).is_platform_admin
        assert not principal(platform_role=PlatformRole.SUPPORT).is_platform_admin
        assert not principal().is_platform_admin


class TestInstrumentServiceAccount:
    """Goal 2: an SA creates on behalf without reading the platform."""

    def test_reaches_datasets_from_its_own_instrument(self):
        sa = principal(member(INSTRUMENT_GID, Role.ADMIN), is_sa=True)
        acl = [(ORCID_GID, Role.OWNER), (PROJECT_GID, Role.ADMIN), (INSTRUMENT_GID, Role.CONTRIBUTOR)]
        assert effective_role(sa, acl) == Role.CONTRIBUTOR
        assert has_permission(sa, acl, Permission.CREATE)

    def test_cannot_edit_a_researchers_dataset_on_the_same_machine(self):
        sa = principal(member(INSTRUMENT_GID, Role.ADMIN), is_sa=True)
        acl = [(ORCID_GID, Role.OWNER), (INSTRUMENT_GID, Role.CONTRIBUTOR)]
        assert not has_permission(sa, acl, Permission.EDIT)
        assert not has_permission(sa, acl, Permission.DELETE)

    def test_cannot_reach_another_instruments_datasets(self):
        sa = principal(member(INSTRUMENT_GID, Role.ADMIN), is_sa=True)
        assert effective_role(sa, [(4242, Role.CONTRIBUTOR)]) is None

    def test_cannot_reach_project_data_it_did_not_produce(self):
        sa = principal(member(INSTRUMENT_GID, Role.ADMIN), is_sa=True)
        assert effective_role(sa, [(PROJECT_GID, Role.ADMIN)]) is None


class TestGroupIdsAtOrAbove:
    """The Python half of the list filter. min(a,b) >= r iff a >= r and b >= r,
    so filtering memberships here lets SQL compare one column."""

    @pytest.mark.parametrize("threshold", ALL_ROLES)
    def test_returns_only_memberships_clearing_the_threshold(self, threshold):
        p = principal(*[member(i, role) for i, role in enumerate(ALL_ROLES, start=10)])
        expected = [i for i, role in enumerate(ALL_ROLES, start=10) if role >= threshold]
        assert p.group_ids_at_or_above(threshold) == expected

    @pytest.mark.parametrize("standing,grant", list(itertools.product(ALL_ROLES, ALL_ROLES)))
    def test_agrees_with_effective_role(self, standing, grant):
        """The decomposition must not diverge from the direct computation."""
        p = principal(member(PROJECT_GID, standing))
        for threshold in ALL_ROLES:
            via_filter = (
                PROJECT_GID in p.group_ids_at_or_above(threshold) and grant >= threshold
            )
            role = effective_role(p, [(PROJECT_GID, grant)])
            via_direct = role is not None and role >= threshold
            assert via_filter == via_direct


class TestCanGrant:
    @pytest.mark.parametrize("actor,target", list(itertools.product(ALL_ROLES, ALL_ROLES)))
    def test_may_grant_at_or_below_own_role_only(self, actor, target):
        expected = actor >= Role.ADMIN and target <= actor
        assert can_grant(actor, target) is expected

    def test_admin_cannot_mint_an_owner(self):
        assert not can_grant(Role.ADMIN, Role.OWNER)

    def test_admin_cannot_demote_an_owner(self):
        """An owner's entry is not modifiable by an admin. Without the
        current_role check the target (viewer) clears the cap on its own and a
        project admin could demote the lead."""
        for target in ALL_ROLES:
            assert not can_grant(Role.ADMIN, target, current_role=Role.OWNER)

    def test_owner_may_modify_an_owner_entry(self):
        assert can_grant(Role.OWNER, Role.VIEWER, current_role=Role.OWNER)

    @pytest.mark.parametrize(
        "actor,current,target",
        list(itertools.product(ALL_ROLES, ALL_ROLES, ALL_ROLES)),
    )
    def test_cap_binds_on_both_the_existing_and_the_new_role(self, actor, current, target):
        expected = actor >= Role.ADMIN and target <= actor and current <= actor
        assert can_grant(actor, target, current_role=current) is expected

    def test_admin_may_modify_entries_at_or_below_admin(self):
        for current in (Role.VIEWER, Role.CONTRIBUTOR, Role.EDITOR, Role.ADMIN):
            assert can_grant(Role.ADMIN, Role.VIEWER, current_role=current)

    def test_editor_cannot_manage_acls_at_all(self):
        for target in ALL_ROLES:
            assert not can_grant(Role.EDITOR, target)

    def test_no_access_cannot_grant(self):
        assert not can_grant(None, Role.VIEWER)


class TestRoleIn:
    def test_finds_a_group_by_name(self):
        p = principal(member(PROJECT_GID, Role.EDITOR, "proj_x"))
        assert p.role_in("proj_x") is Role.EDITOR

    def test_returns_none_for_a_group_not_joined(self):
        p = principal(member(PROJECT_GID, Role.EDITOR, "proj_x"))
        assert p.role_in("proj_y") is None


class TestPrincipalIsAValue:
    def test_is_frozen(self):
        p = principal(member(PROJECT_GID, Role.VIEWER))
        with pytest.raises(Exception):
            p.user_id = "someone-else"

    def test_equal_principals_compare_equal(self):
        a = principal(member(PROJECT_GID, Role.VIEWER))
        b = principal(member(PROJECT_GID, Role.VIEWER))
        assert a == b

    def test_defaults_are_least_privilege(self):
        p = Principal(user_id="0000-0001")
        assert p.platform_role is PlatformRole.NONE
        assert p.is_service_account is False
        assert p.groups == ()
        assert effective_role(p, [(PROJECT_GID, Role.OWNER)]) is None


class TestGrantFloor:
    """The floor differs by what is being managed: ACL entries share data
    outward without bound and need `admin`; project membership is bounded by
    the project and needs only `editor`."""

    def test_editor_cannot_manage_acl_entries(self):
        for target in ALL_ROLES:
            assert not can_grant(Role.EDITOR, target)          # default floor = admin

    @pytest.mark.parametrize("target", [Role.VIEWER, Role.CONTRIBUTOR, Role.EDITOR])
    def test_editor_can_seat_members_at_or_below(self, target):
        assert can_grant(Role.EDITOR, target, floor=Role.EDITOR)

    @pytest.mark.parametrize("target", [Role.ADMIN, Role.OWNER])
    def test_editor_still_cannot_seat_above_itself(self, target):
        assert not can_grant(Role.EDITOR, target, floor=Role.EDITOR)

    def test_contributor_cannot_seat_anyone(self):
        for target in ALL_ROLES:
            assert not can_grant(Role.CONTRIBUTOR, target, floor=Role.EDITOR)

    def test_lowering_the_floor_does_not_lift_the_cap(self):
        """The two are independent — floor gates who may act, cap gates how high."""
        assert not can_grant(Role.EDITOR, Role.OWNER, floor=Role.VIEWER)
        assert not can_grant(Role.ADMIN, Role.OWNER, floor=Role.VIEWER)
