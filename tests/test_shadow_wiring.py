"""Shadow mode must actually receive the principal through a real request.

The unit tests set current_principal directly, so they passed while shadow mode
recorded nothing in staging: FastAPI runs a *sync* dependency in a threadpool
with a copied context, so a ContextVar set inside one never reaches the route
handler. authorize_user is async for exactly this reason.

These tests go through TestClient so the plumbing is exercised, not mocked.
"""
from contextvars import ContextVar
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient

_probe: ContextVar = ContextVar("_probe", default=None)


def _blocking_work():
    return "principal"


class TestContextVarPropagation:
    """The mechanism shadow mode depends on."""

    def test_sync_dependency_does_not_propagate(self):
        """The bug. Kept as a test so nobody 'simplifies' authorize_user back."""
        app = FastAPI()

        def sync_dep():
            _probe.set(_blocking_work())
            return "x"

        @app.get("/r")
        def route(_: Annotated[str, Depends(sync_dep)]):
            return {"seen": _probe.get()}

        assert TestClient(app).get("/r").json() == {"seen": None}

    @pytest.mark.parametrize("route_is_async", [False, True])
    def test_async_dependency_propagates(self, route_is_async):
        """The fix: set it in the request's own context, keep the blocking work
        in a threadpool."""
        app = FastAPI()

        async def async_dep():
            value = await run_in_threadpool(_blocking_work)
            _probe.set(value)
            return value

        if route_is_async:
            @app.get("/r")
            async def route(_: Annotated[str, Depends(async_dep)]):
                return {"seen": _probe.get()}
        else:
            @app.get("/r")
            def route(_: Annotated[str, Depends(async_dep)]):
                return {"seen": _probe.get()}

        assert TestClient(app).get("/r").json() == {"seen": "principal"}


class TestAuthorizeUserIsAsync:
    def test_authorize_user_is_a_coroutine_function(self):
        """If this becomes sync again, shadow mode silently stops recording —
        no error, no log, just zero output that looks like 'no mismatches'."""
        import inspect
        from app.utils.auth_utils import authorize_user
        assert inspect.iscoroutinefunction(authorize_user)

    def test_the_blocking_worker_is_still_sync(self):
        """The DB work must not run on the event loop."""
        import inspect
        from app.utils.auth_utils import _authorize_user
        assert not inspect.iscoroutinefunction(_authorize_user)


class TestShadowSeesThePrincipal:
    def test_principal_reaches_a_sync_route_through_the_real_dependency(self):
        """End to end: what authorize_user sets is what a route observes."""
        from app.utils import shadow
        from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role

        expected = Principal(
            user_id="0000-0001", platform_role=PlatformRole.NONE,
            groups=(GroupMembership(1, "proj_x", Role.EDITOR),),
        )

        app = FastAPI()

        async def fake_authorize_user():
            principal = await run_in_threadpool(lambda: expected)
            shadow.current_principal.set(principal)
            return principal

        @app.get("/r")
        def route(_: Annotated[Principal, Depends(fake_authorize_user)]):
            seen = shadow.current_principal.get()
            return {"user_id": seen.user_id if seen else None,
                    "groups": len(seen.groups) if seen else 0}

        assert TestClient(app).get("/r").json() == {"user_id": "0000-0001", "groups": 1}
