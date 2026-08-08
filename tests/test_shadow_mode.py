"""Shadow mode must observe and never interfere.

Phase 3 of docs/permission_system_plan.md. The entire value depends on two
properties: the old answer is what the caller acts on, and a bug in the new
path cannot break a request. Both are asserted here.
"""
import logging

import pytest

from app.utils import shadow
from app.utils.permissions import (
    GroupMembership, Permission, PlatformRole, Principal, Role,
)

PROJECT_GID = 1


def principal(*memberships, platform_role=PlatformRole.NONE):
    return Principal(user_id="0000-0001", platform_role=platform_role,
                     groups=tuple(memberships))


class FakeSession:
    """Returns fixed ACL rows; records how many times it was queried."""

    def __init__(self, rows=(), raises=False):
        self.rows = list(rows)
        self.raises = raises
        self.queries = 0

    def execute(self, *_args, **_kwargs):
        self.queries += 1
        if self.raises:
            raise RuntimeError("boom")
        return self

    def all(self):
        return self.rows


@pytest.fixture(autouse=True)
def clean_shadow(monkeypatch):
    shadow.reset()
    monkeypatch.setattr(shadow, "SHADOW_MODE", True)
    token = shadow.current_principal.set(None)
    yield
    shadow.current_principal.reset(token)
    shadow.reset()


class TestDisabledByDefault:
    def test_does_nothing_when_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(shadow, "SHADOW_MODE", False)
        session = FakeSession([(PROJECT_GID, "owner")])
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        shadow.compare(session, "check", "mfid-1", Permission.READ, True)
        assert session.queries == 0, "must not even query when disabled"
        assert shadow.report() == {}


class TestAgreement:
    def test_match_is_counted_and_not_logged(self, caplog):
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        session = FakeSession([(PROJECT_GID, "owner")])
        with caplog.at_level(logging.WARNING):
            shadow.compare(session, "require_read_access", "mfid-1", Permission.READ, True)
        assert shadow.report() == {"require_read_access/read": {"match": 1}}
        # The event, not the substring — the summary line contains "mismatch=0".
        assert "permission_shadow_mismatch" not in caplog.text

    def test_mismatch_is_counted_and_logged(self, caplog):
        """Old said yes, the new model says no."""
        shadow.current_principal.set(principal())  # no groups, so no access
        session = FakeSession([(PROJECT_GID, "owner")])
        with caplog.at_level(logging.WARNING):
            shadow.compare(session, "require_read_access", "mfid-1", Permission.READ, True)
        assert shadow.report() == {"require_read_access/read": {"mismatch": 1}}
        assert "permission_shadow_mismatch" in caplog.text
        assert "mfid-1" in caplog.text

    def test_mismatch_the_other_way_is_also_caught(self, caplog):
        """New allows where old denied — the dangerous direction."""
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        session = FakeSession([(PROJECT_GID, "owner")])
        with caplog.at_level(logging.WARNING):
            shadow.compare(session, "require_read_access", "mfid-1", Permission.READ, False)
        assert shadow.report() == {"require_read_access/read": {"mismatch": 1}}
        assert "old=False new=True" in caplog.text


class TestNeverInterferes:
    def test_a_broken_new_path_does_not_raise(self, caplog):
        """The load-bearing property: an exception here must not reach the route."""
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        session = FakeSession(raises=True)
        with caplog.at_level(logging.WARNING):
            shadow.compare(session, "require_read_access", "mfid-1", Permission.READ, True)
        assert shadow.report() == {"require_read_access/read": {"error": 1}}
        assert "permission_shadow_error" in caplog.text

    def test_missing_principal_is_silent(self):
        """Background tasks and non-request callers have no principal."""
        session = FakeSession([(PROJECT_GID, "owner")])
        shadow.compare(session, "require_read_access", "mfid-1", Permission.READ, True)
        assert shadow.report() == {}
        assert session.queries == 0

    def test_compare_returns_none_always(self):
        """It reports; it does not decide."""
        shadow.current_principal.set(principal())
        session = FakeSession([(PROJECT_GID, "owner")])
        assert shadow.compare(session, "c", "m", Permission.READ, True) is None
        assert shadow.compare(session, "c", "m", Permission.READ, False) is None


class TestCounters:
    def test_counts_split_by_check_and_permission(self):
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        session = FakeSession([(PROJECT_GID, "owner")])
        shadow.compare(session, "require_read_access", "m1", Permission.READ, True)
        shadow.compare(session, "require_write_access", "m1", Permission.EDIT, True)
        shadow.compare(session, "require_write_access", "m2", Permission.EDIT, True)
        assert shadow.report() == {
            "require_read_access/read": {"match": 1},
            "require_write_access/edit": {"match": 2},
        }

    def test_match_counts_are_what_prove_a_route_was_exercised(self):
        """'No mismatches' is meaningless without them — the cutover gate is
        per-check match counts, not the absence of complaints."""
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        session = FakeSession([(PROJECT_GID, "owner")])
        for _ in range(5):
            shadow.compare(session, "require_read_access", "m", Permission.READ, True)
        assert shadow.report()["require_read_access/read"]["match"] == 5

    def test_first_check_emits_a_summary(self, caplog):
        """Confirmation that it is running, without waiting for 500 checks."""
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        session = FakeSession([(PROJECT_GID, "owner")])
        with caplog.at_level(logging.WARNING):
            shadow.compare(session, "require_read_access", "m", Permission.READ, True)
        assert "permission_shadow_summary" in caplog.text

    def test_summary_is_emitted_periodically(self, caplog, monkeypatch):
        monkeypatch.setattr(shadow, "SUMMARY_EVERY", 3)
        shadow.current_principal.set(principal(GroupMembership(PROJECT_GID, "p", Role.OWNER)))
        session = FakeSession([(PROJECT_GID, "owner")])
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                shadow.compare(session, "require_read_access", "m", Permission.READ, True)
        assert "permission_shadow_summary" in caplog.text


class TestPlatformAdminBypass:
    def test_promoted_service_account_agrees_with_the_old_bypass(self):
        """The transitional state: SA holds platform_role=admin, old check let
        it through via the admin group. Both say yes, so no mismatch noise."""
        sa = Principal(user_id="sa-1", platform_role=PlatformRole.ADMIN,
                       is_service_account=True, groups=())
        shadow.current_principal.set(sa)
        session = FakeSession([])  # no ACL rows at all
        shadow.compare(session, "require_read_access", "m", Permission.READ, True)
        assert shadow.report() == {"require_read_access/read": {"match": 1}}

    def test_would_mismatch_without_the_promotion(self):
        """What the counters would show the day platform_role is revoked."""
        sa = Principal(user_id="sa-1", platform_role=PlatformRole.NONE,
                       is_service_account=True, groups=())
        shadow.current_principal.set(sa)
        session = FakeSession([])
        shadow.compare(session, "require_read_access", "m", Permission.READ, True)
        assert shadow.report() == {"require_read_access/read": {"mismatch": 1}}
