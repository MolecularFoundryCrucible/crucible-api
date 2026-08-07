"""Agreement tests: the old checks vs the new ones, across a matrix.

The test-time counterpart to shadow-mode validation. Shadow mode compares the
two checks against production traffic; this compares them against a fixture
where every intended difference can be written down in advance.

Both sides are computed honestly:

  old answer = the pre-role helpers, against PRE-migration data
  new answer = has_permission, against POST-migration data

Running the old check against migrated data would prove nothing — the backfill
deletes the admin and read_only rows those checks depend on.

Anything not in EXPECTED_CHANGES must agree. A new disagreement is a
regression; a listed one that stops happening is a stale expectation, and is
also failed, so this file cannot rot into a list of excuses.

Skipped unless CRUCIBLE_TEST_PG_URL is set. See tests/pg_fixture.py.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import Session, create_engine

from app.utils.permissions import Permission, has_permission
from tests.pg_fixture import (
    ALL_PRINCIPALS, ALL_RESOURCES, LEAD, MEMBER, PG_URL, PLATFORM_ADMIN, SA,
    STAFF, acl_for, drop_new_columns, fresh_schema, group_ids_for,
    principal_for, run_backfill, run_migration_sql, seed,
)

pytestmark = pytest.mark.skipif(not PG_URL, reason="CRUCIBLE_TEST_PG_URL not set")

# Datasets and samples only. Projects were not resources before the migration,
# so there is no old answer to compare against.
COMPARABLE_RESOURCES = [r for r in ALL_RESOURCES if r != "proj_x"]

# What the old check actually gated, mapped onto the new vocabulary.
# Resource deletion was require_admin in deletion.py, not a write check, so
# `delete` is compared against platform-admin status rather than write access.
OPERATIONS = ["read", "write"]


# Every intended behaviour change, as (principal, resource, operation) -> why.
# Keyed deliberately at full granularity: a blanket "staff loses access" entry
# would hide a case where they lost something nobody meant them to.
EXPECTED_CHANGES = {
    (STAFF, "ds-pub", "write"):
        "instrument groups now hold only the service account, so a beamline "
        "scientist loses write on datasets from their machine. Read survives "
        "here only because ds-pub is public — on a private dataset they lose "
        "both, which the agreement above already shows as no change since "
        "they never had access to ds-priv",
    (SA, "ds-pub", "write"):
        "the instrument grant is contributor, so the service account can "
        "still create but no longer edit a researcher's dataset taken on the "
        "same machine",

    # The service account is in the admin group — on staging, every
    # admin-group member is one. That membership gave it read and write on
    # every resource through the admin ACL rows. Those rows are deleted and it
    # is deliberately not promoted to platform_role, so the platform-wide
    # access goes away. This is goal (2) of the redesign, and it is also the
    # change that breaks live instruments if they are not bound to their
    # instruments first — see the sequencing in the migration path.
    (SA, "ds-priv", "read"):
        "loses admin-group bypass; ds-priv is private and its instrument is "
        "not xrd-1, so nothing else reaches it",
    (SA, "ds-priv", "write"):
        "loses admin-group bypass",
    (SA, "sm-pub", "write"):
        "loses admin-group bypass; the public group grants viewer only, so "
        "read survives and write does not",
}


def _old_read(engine, orcid, mfid):
    from app.utils.auth_utils import require_read_access
    ag_ids = group_ids_for(engine, orcid)
    with Session(engine) as session:
        try:
            require_read_access(session, mfid, ag_ids)
            return True
        except HTTPException:
            return False


def _old_write(engine, orcid, mfid):
    from app.utils.auth_utils import require_write_access
    ag_ids = group_ids_for(engine, orcid)
    with Session(engine) as session:
        try:
            require_write_access(session, ag_ids, [mfid])
            return True
        except HTTPException:
            return False


OLD_CHECK = {"read": _old_read, "write": _old_write}
NEW_PERMISSION = {"read": Permission.READ, "write": Permission.EDIT}


@pytest.fixture(scope="module")
def answers():
    """Old answers before migrating, new answers after, from one database."""
    engine = create_engine(PG_URL)
    fresh_schema(engine)
    with Session(engine) as session:
        seed(session)

    old = {}
    for orcid in ALL_PRINCIPALS:
        for mfid in COMPARABLE_RESOURCES:
            for op in OPERATIONS:
                old[(orcid, mfid, op)] = OLD_CHECK[op](engine, orcid, mfid)

    drop_new_columns(engine)
    run_migration_sql(engine)
    run_backfill(engine)

    new = {}
    for orcid in ALL_PRINCIPALS:
        principal = principal_for(engine, orcid)
        for mfid in COMPARABLE_RESOURCES:
            acl = acl_for(engine, mfid)
            for op in OPERATIONS:
                new[(orcid, mfid, op)] = has_permission(principal, acl, NEW_PERMISSION[op])

    return old, new


@pytest.mark.parametrize("orcid", ALL_PRINCIPALS)
@pytest.mark.parametrize("mfid", COMPARABLE_RESOURCES)
@pytest.mark.parametrize("op", OPERATIONS)
def test_old_and_new_agree(answers, orcid, mfid, op):
    old, new = answers
    key = (orcid, mfid, op)
    if key in EXPECTED_CHANGES:
        pytest.skip(f"intended change: {EXPECTED_CHANGES[key]}")
    assert old[key] == new[key], (
        f"undeclared behaviour change for {orcid} / {mfid} / {op}: "
        f"was {old[key]}, now {new[key]}. Either this is a regression, or it "
        f"belongs in EXPECTED_CHANGES with a reason."
    )


def test_every_expected_change_actually_happens(answers):
    """A listed exception that no longer differs is a stale excuse. Failing on
    it is what stops this file decaying into a permanent allowlist."""
    old, new = answers
    stale = [key for key in EXPECTED_CHANGES if old[key] == new[key]]
    assert not stale, (
        "these are listed as intended changes but the answers now agree — "
        f"remove them from EXPECTED_CHANGES: {stale}"
    )


def test_platform_admin_keeps_full_access(answers):
    """The bypass moved from materialised ACL rows to platform_role. The
    answers must not move with it."""
    old, new = answers
    for mfid in COMPARABLE_RESOURCES:
        for op in OPERATIONS:
            key = (PLATFORM_ADMIN, mfid, op)
            assert old[key] is True and new[key] is True, f"{key}: {old[key]} -> {new[key]}"


def test_no_principal_silently_gains_write(answers):
    """The migration is allowed to remove access. Anything that gains write
    without being declared is the dangerous direction."""
    old, new = answers
    gained = [
        key for key, was in old.items()
        if key[2] == "write" and not was and new[key] and key not in EXPECTED_CHANGES
    ]
    assert not gained, f"undeclared write escalation: {gained}"


def test_project_members_keep_edit_on_project_data(answers):
    """The reason the backfill assigns editor rather than contributor: today
    any project member can edit any project resource, and the migration is not
    supposed to take that away on day one."""
    old, new = answers
    for mfid in COMPARABLE_RESOURCES:
        key = (MEMBER, mfid, "write")
        assert old[key] == new[key] is True, f"{key}: {old[key]} -> {new[key]}"


def test_lead_keeps_everything_they_had(answers):
    old, new = answers
    for mfid in COMPARABLE_RESOURCES:
        for op in OPERATIONS:
            key = (LEAD, mfid, op)
            assert old[key] == new[key], f"{key}: {old[key]} -> {new[key]}"
