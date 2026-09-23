import json
import uuid

import pytest
from sqlmodel import Session

from app.api.main import app
from app.models.core_models import User
from app.utils.permissions import (
    PlatformRole,
    Principal,
    PrivilegeMode,
    PrivilegeModeSource,
    resolve_authorization_context,
)


def _seed_user(db_engine, unique_id: str, *, is_service_account: bool = False):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Privilege",
            last_name="Tester",
            email=None,
            unique_id=unique_id,
            username=f"{unique_id}-username",
            is_service_account=is_service_account,
            platform_role=PlatformRole.ADMIN.value,
        ))
        session.commit()


def test_omitted_mode_preserves_platform_admin_behavior():
    actor = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    context = resolve_authorization_context(actor, None)

    assert context.actor is actor
    assert context.principal is actor
    assert context.privilege_mode is PrivilegeMode.ELEVATED
    assert context.mode_source is PrivilegeModeSource.COMPATIBILITY_DEFAULT
    assert context.can_elevate is True


def test_normal_mode_suppresses_only_platform_admin_role():
    actor = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    context = resolve_authorization_context(actor, PrivilegeMode.NORMAL)

    assert context.actor is actor
    assert context.principal.user_id == actor.user_id
    assert context.principal.groups == actor.groups
    assert context.principal.platform_role is PlatformRole.NONE
    assert context.privilege_mode is PrivilegeMode.NORMAL
    assert context.mode_source is PrivilegeModeSource.EXPLICIT
    assert context.can_elevate is True


def test_normal_mode_preserves_platform_contributor_role():
    actor = Principal(
        user_id="contributor",
        platform_role=PlatformRole.CONTRIBUTOR,
    )

    context = resolve_authorization_context(actor, PrivilegeMode.NORMAL)

    assert context.principal is actor
    assert context.principal.platform_role is PlatformRole.CONTRIBUTOR
    assert context.can_elevate is False


@pytest.mark.parametrize(
    "actor",
    [
        Principal(user_id="ordinary"),
        Principal(
            user_id="contributor",
            platform_role=PlatformRole.CONTRIBUTOR,
        ),
        Principal(
            user_id="service-admin",
            platform_role=PlatformRole.ADMIN,
            is_service_account=True,
        ),
    ],
)
def test_ineligible_principals_cannot_explicitly_elevate(actor):
    with pytest.raises(PermissionError):
        resolve_authorization_context(actor, PrivilegeMode.ELEVATED)


def test_admin_profile_reports_eligibility_and_normal_capabilities(
    client,
    db_engine,
    auth_as,
):
    _seed_user(db_engine, "profile-admin")
    auth_as("profile-admin", platform_role=PlatformRole.ADMIN)

    response = client.get(
        "/account/profile",
        headers={"Crucible-Privilege-Mode": "normal"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization"] == {
        "platform_role": "admin",
        "effective_privilege_mode": "normal",
        "can_elevate": True,
    }
    assert body["capabilities"]["can_manage_service_accounts"] is False
    assert body["capabilities"]["can_create_for_others"] is False
    assert response.headers["Crucible-Privilege-Mode"] == "normal"
    assert response.headers["Cache-Control"] == "private"
    assert "Authorization" in response.headers["Vary"]
    assert "Crucible-Privilege-Mode" in response.headers["Vary"]
    uuid.UUID(response.headers["X-Request-ID"])


@pytest.mark.parametrize("mode", [None, "elevated"])
def test_admin_profile_preserves_legacy_or_explicit_elevation(
    client,
    db_engine,
    auth_as,
    mode,
):
    _seed_user(db_engine, f"profile-admin-{mode or 'omitted'}")
    auth_as(
        f"profile-admin-{mode or 'omitted'}",
        platform_role=PlatformRole.ADMIN,
    )
    headers = {"Crucible-Privilege-Mode": mode} if mode is not None else {}

    response = client.get("/account/profile", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["authorization"] == {
        "platform_role": "admin",
        "effective_privilege_mode": "elevated",
        "can_elevate": True,
    }
    assert response.json()["capabilities"]["can_manage_service_accounts"] is True
    assert response.json()["capabilities"]["can_create_for_others"] is True
    assert response.headers["Crucible-Privilege-Mode"] == "elevated"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_normal_and_elevated_lanes_reach_admin_route_differently(
    client,
    auth_as,
):
    auth_as("route-admin", platform_role=PlatformRole.ADMIN)

    normal = client.get(
        "/service_accounts",
        headers={"Crucible-Privilege-Mode": "normal"},
    )
    elevated = client.get(
        "/service_accounts",
        headers={"Crucible-Privilege-Mode": "elevated"},
    )

    assert normal.status_code == 403
    assert normal.headers["Crucible-Privilege-Mode"] == "normal"
    assert elevated.status_code == 200, elevated.text
    assert elevated.headers["Crucible-Privilege-Mode"] == "elevated"


@pytest.mark.parametrize(
    "platform_role,is_service_account",
    [
        (PlatformRole.NONE, False),
        (PlatformRole.CONTRIBUTOR, False),
        (PlatformRole.ADMIN, True),
    ],
)
def test_explicit_elevation_rejects_ineligible_callers(
    client,
    auth_as,
    platform_role,
    is_service_account,
):
    auth_as(
        "ineligible",
        platform_role=platform_role,
        is_service_account=is_service_account,
    )

    response = client.get(
        "/account/profile",
        headers={"Crucible-Privilege-Mode": "elevated"},
    )

    assert response.status_code == 403
    assert "Crucible-Privilege-Mode" not in response.headers
    uuid.UUID(response.headers["X-Request-ID"])


def test_invalid_mode_is_rejected(client, auth_as):
    auth_as("ordinary")

    response = client.get(
        "/account/profile",
        headers={"Crucible-Privilege-Mode": "sometimes"},
    )

    assert response.status_code == 422
    assert "Crucible-Privilege-Mode" not in response.headers


def test_elevated_request_emits_structured_audit_record(
    client,
    auth_as,
    caplog,
):
    auth_as("audit-admin", platform_role=PlatformRole.ADMIN)

    with caplog.at_level("INFO", logger="app.api.main"):
        response = client.get(
            "/service_accounts",
            headers={"Crucible-Privilege-Mode": "elevated"},
        )

    records = [record for record in caplog.records if hasattr(record, "audit")]
    assert response.status_code == 200
    assert len(records) == 1
    payload = records[0].audit
    assert payload["actor_id"] == "audit-admin"
    assert payload["privilege_mode"] == "elevated"
    assert payload["mode_source"] == "explicit"
    assert payload["method"] == "GET"
    assert payload["route"] == "/service_accounts"
    assert payload["status"] == 200
    assert json.loads(records[0].message) == payload


def test_rejected_elevation_emits_structured_audit_record(
    client,
    auth_as,
    caplog,
):
    auth_as("audit-ordinary")

    with caplog.at_level("INFO", logger="app.api.main"):
        response = client.get(
            "/service_accounts",
            headers={"Crucible-Privilege-Mode": "elevated"},
        )

    records = [record for record in caplog.records if hasattr(record, "audit")]
    assert response.status_code == 403
    assert len(records) == 1
    assert records[0].audit["actor_id"] == "audit-ordinary"
    assert records[0].audit["mode_source"] == "explicit"
    assert records[0].audit["status"] == 403


def test_omitted_admin_mode_is_audited_as_compatibility_default(
    client,
    auth_as,
    caplog,
):
    auth_as("legacy-admin", platform_role=PlatformRole.ADMIN)

    with caplog.at_level("INFO", logger="app.api.main"):
        response = client.get("/service_accounts")

    records = [record for record in caplog.records if hasattr(record, "audit")]
    assert response.status_code == 200
    assert len(records) == 1
    assert records[0].audit["mode_source"] == "compatibility_default"


def test_normal_request_does_not_emit_privileged_audit_record(
    client,
    auth_as,
    caplog,
):
    auth_as("normal-admin", platform_role=PlatformRole.ADMIN)

    with caplog.at_level("INFO", logger="app.api.main"):
        response = client.get(
            "/service_accounts",
            headers={"Crucible-Privilege-Mode": "normal"},
        )

    assert response.status_code == 403
    assert not [record for record in caplog.records if hasattr(record, "audit")]


def test_openapi_publishes_privilege_mode_contract():
    app.openapi_schema = None
    schema = app.openapi()
    operation = schema["paths"]["/account/profile"]["get"]
    parameters = {
        parameter["name"].lower(): parameter
        for parameter in operation["parameters"]
    }

    parameter = parameters["crucible-privilege-mode"]
    assert parameter["in"] == "header"
    assert parameter["required"] is False
    assert operation["responses"]["200"]["headers"]["Crucible-Privilege-Mode"] == {
        "$ref": "#/components/headers/CruciblePrivilegeMode"
    }
    assert operation["responses"]["200"]["headers"]["X-Request-ID"] == {
        "$ref": "#/components/headers/CrucibleRequestId"
    }
    assert operation["responses"]["200"]["headers"]["Cache-Control"] == {
        "$ref": "#/components/headers/PrivateCacheControl"
    }
    assert operation["responses"]["200"]["headers"]["Vary"] == {
        "$ref": "#/components/headers/PrivilegeModeVary"
    }
    assert "authorization" in schema["components"]["schemas"]["AccountProfileRead"]["required"]
    readiness = schema["paths"]["/health/ready"]["get"]
    assert all(
        parameter["name"].lower() != "crucible-privilege-mode"
        for parameter in readiness.get("parameters", [])
    )
    assert readiness["responses"]["200"]["headers"] == {
        "X-Request-ID": {"$ref": "#/components/headers/CrucibleRequestId"}
    }
