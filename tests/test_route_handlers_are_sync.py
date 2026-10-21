import inspect

from fastapi.routing import APIRoute

from app.api.main import app


def test_route_handlers_are_plain_functions():
    async_routes = sorted(
        f"{','.join(route.methods)} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute) and inspect.iscoroutinefunction(route.endpoint)
    )
    assert async_routes == [], "Handlers that do blocking database or storage work must be `def` so FastAPI runs them in its thread pool"
