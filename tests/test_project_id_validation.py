"""project_id must not be able to land in the access-group namespace.

`create_project` derives the project's access group from the caller-supplied
`project_id` via `add_or_update`, which **reuses** an existing group rather
than failing. Before this validation, `POST /projects {"project_id": "admin"}`
appended the named lead to the admin access group — a one-request path from any
authenticated user to platform administrator, on a route with no authorization
check at all.

These tests are the guard. The real fix is naming groups after MFIDs so a
caller-supplied string never reaches the namespace; see the plan.
"""
import pydantic
import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.models.core_models import AccessGroup, Instrument, Project, User
from app.models.projects import ProjectCreate
from app.utils.project_utils import (
    RESERVED_PROJECT_IDS, assert_project_id_available, validate_project_id,
)

# Representative production project IDs within the new length range.
REAL_PROJECT_IDS = [
    "MFP00001", "MFP10928", "10k_perovskites", "11k-project", "4D_STEM",
    "AUM_DEMO", "nsf-summer-school-2026",
    "crucible-developers", "LPS_test", "LPS-giwaxs", "qspleem-test",
    "SummerSchool_DEMO_RD", "TEST000002", "MFUSER_brad",
]

MALFORMED = [
    ("", "empty"),
    ("a", "single character"),
    ("ab", "two characters"),
    ("[7093]", "brackets"),
    ("Training [9069]", "brackets and spaces"),
    ("SummerSchool DEMO", "space"),
    ("-leading", "leading hyphen"),
    ("_leading", "leading underscore"),
    ("has/slash", "slash"),
    ("x" * 26, "too long"),
    ("proj_x\n", "trailing newline"),
]


class TestGrammar:
    @pytest.mark.parametrize("project_id", REAL_PROJECT_IDS)
    def test_real_project_ids_are_accepted(self, project_id):
        assert validate_project_id(project_id) == project_id

    @pytest.mark.parametrize("project_id,why", MALFORMED)
    def test_malformed_is_rejected(self, project_id, why):
        with pytest.raises(ValueError):
            validate_project_id(project_id)

    def test_case_is_preserved(self):
        """MFP IDs are uppercase and meaningful; do not lowercase them."""
        assert validate_project_id("MFP00001") == "MFP00001"

    @pytest.mark.parametrize("project_id", ["abc", "x" * 25])
    def test_length_boundaries_are_accepted(self, project_id):
        assert validate_project_id(project_id) == project_id


class TestReservedNames:
    @pytest.mark.parametrize("name", sorted(RESERVED_PROJECT_IDS))
    def test_reserved_names_are_rejected(self, name):
        with pytest.raises(ValueError, match="reserved"):
            validate_project_id(name)

    @pytest.mark.parametrize("name", ["ADMIN", "Admin", "PUBLIC", "Read_Only"])
    def test_reserved_check_is_case_insensitive(self, name):
        """`PUBLIC` would create a group distinct from `public` — not directly
        exploitable, but confusing enough to refuse."""
        with pytest.raises(ValueError, match="reserved"):
            validate_project_id(name)

    def test_the_escalation_payload_is_rejected(self):
        """The actual attack: become a platform admin in one request."""
        with pytest.raises(pydantic.ValidationError):
            ProjectCreate(
                project_id="admin",
                organization="LBNL",
                project_lead_username="attacker",
            )

    def test_the_disclosure_payload_is_rejected(self):
        """The mirror image: make a project's data world-readable."""
        with pytest.raises(pydantic.ValidationError):
            ProjectCreate(
                project_id="public",
                organization="LBNL",
                project_lead_username="anyone",
            )


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(
        engine, tables=[m.__table__ for m in (AccessGroup, Project, User, Instrument)])
    with Session(engine) as s:
        yield s


class TestCollisionWithExistingGroups:
    """After the duplicate-project check, any group holding the name belongs to
    something else. One lookup covers every case."""

    def test_free_name_is_allowed(self, session):
        assert_project_id_available(session, "brand_new_project")  # must not raise

    @pytest.mark.parametrize("group_name,what", [
        ("0000-0001-2345-6789", "a user's ORCID group"),
        ("xrd-1", "an instrument group"),
        ("admin", "a reserved group"),
        ("xrd-typo", "a typo-created orphan group"),
    ])
    def test_collision_is_rejected(self, session, group_name, what):
        session.add(AccessGroup(group_name=group_name))
        session.commit()
        with pytest.raises(HTTPException) as exc:
            assert_project_id_available(session, group_name)
        assert exc.value.status_code == 409
        assert "collides" in exc.value.detail

    def test_a_user_cannot_hijack_someone_elses_orcid_group(self, session):
        """Reusing an ORCID group would put the project's members into it,
        breaking one_owner_per_group and the assumption that everyone holds
        owner in their own group — which is what makes sharing safe."""
        session.add(User(first_name="A", last_name="B", email="a@x",
                         unique_id="0000-0002-9999-0000"))
        session.add(AccessGroup(group_name="0000-0002-9999-0000"))
        session.commit()
        with pytest.raises(HTTPException):
            assert_project_id_available(session, "0000-0002-9999-0000")


class TestCollisionIsCaseInsensitive:
    def test_group_name_collision_ignores_case(self, session):
        session.add(AccessGroup(group_name="xrd-1"))
        session.commit()
        with pytest.raises(HTTPException):
            assert_project_id_available(session, "XRD-1")

    def test_instrument_name_does_not_block_a_project_id(self, session):
        """instrument_name is free-text display, not an identifier."""
        session.add(Instrument(instrument_name="xrd-2", owner="lab", location="b30"))
        session.commit()
        assert_project_id_available(session, "XRD-2")


def test_grammar_and_collision_are_separate_layers(session):
    """A well-formed name can still collide, so both checks are needed."""
    validate_project_id("xrd-1")  # grammar is fine
    session.add(AccessGroup(group_name="xrd-1"))
    session.commit()
    with pytest.raises(HTTPException):
        assert_project_id_available(session, "xrd-1")
