import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from prometheus_fastapi_instrumentator import Instrumentator

from app.build_info import api_version
from app.api.dependencies import get_api_root_path
from app.utils.auth_utils import PRIVILEGE_MODE_HEADER
from app.utils.permissions import PrivilegeMode, PrivilegeModeSource
from app.api.routes import (account, auth, files, datasets, gcs_storage, resources,
                            samples, instruments, users, projects,
                            access_groups, entities, misc, deletion,
                            ingestion_requests, ai, service_accounts,
                            resource_graph)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger(__name__)

api_root_path = get_api_root_path()
app = FastAPI(
    root_path=api_root_path,
    root_path_in_servers=False,
    servers=[{"url": api_root_path}],
    version=api_version(),
)


def _append_vary(response, field_name: str) -> None:
    values = [
        value.strip()
        for value in response.headers.get("Vary", "").split(",")
        if value.strip()
    ]
    if field_name.lower() not in {value.lower() for value in values}:
        values.append(field_name)
    response.headers["Vary"] = ", ".join(values)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


def _target_mfid(request: Request):
    for key, value in request.path_params.items():
        if key == "mfid" or key.endswith("_mfid"):
            return str(value)
    return None


def _audit_privileged_request(
    request: Request,
    status_code: int,
    duration_ms: float,
) -> None:
    context = getattr(request.state, "authorization_context", None)
    requested_mode = getattr(request.state, "requested_privilege_mode", None)
    if context is None and requested_mode is not PrivilegeMode.ELEVATED:
        return
    if context is not None and context.privilege_mode is not PrivilegeMode.ELEVATED:
        return

    actor = (
        context.actor
        if context is not None
        else getattr(request.state, "authenticated_actor", None)
    )
    mode_source = (
        context.mode_source
        if context is not None
        else getattr(
            request.state,
            "privilege_mode_source",
            PrivilegeModeSource.EXPLICIT,
        )
    )
    payload = {
        "event": "privileged_request",
        "actor_id": actor.user_id if actor is not None else None,
        "privilege_mode": PrivilegeMode.ELEVATED.value,
        "mode_source": mode_source.value,
        "method": request.method,
        "route": _route_template(request),
        "target_mfid": _target_mfid(request),
        "request_id": request.state.request_id,
        "status": status_code,
        "duration_ms": round(duration_ms, 3),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        extra={"audit": payload},
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "%s %s %s %.3fms request_id=%s",
            request.method,
            _route_template(request),
            500,
            duration_ms,
            request.state.request_id,
        )
        _audit_privileged_request(request, 500, duration_ms)
        raise

    context = getattr(request.state, "authorization_context", None)
    if context is not None:
        response.headers[PRIVILEGE_MODE_HEADER] = context.privilege_mode.value
        _append_vary(response, "Authorization")
        _append_vary(response, PRIVILEGE_MODE_HEADER)
        if context.privilege_mode is PrivilegeMode.ELEVATED:
            response.headers["Cache-Control"] = "private, no-store"
        elif "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "private"
    response.headers["X-Request-ID"] = request.state.request_id
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "%s %s %s %.3fms request_id=%s",
        request.method,
        _route_template(request),
        response.status_code,
        duration_ms,
        request.state.request_id,
    )
    _audit_privileged_request(request, response.status_code, duration_ms)
    return response


Instrumentator().instrument(app).expose(app)

app.include_router(account.router)
app.include_router(auth.router)
app.include_router(files.router)
app.include_router(ingestion_requests.router)
app.include_router(datasets.router)
app.include_router(gcs_storage.router)
app.include_router(resources.router)
app.include_router(resource_graph.router)
app.include_router(samples.router)
app.include_router(instruments.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(access_groups.router)
app.include_router(entities.router)
app.include_router(misc.router)
app.include_router(deletion.router)
app.include_router(ai.router)
app.include_router(service_accounts.router)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        summary=app.summary,
        description=app.description,
        terms_of_service=app.terms_of_service,
        contact=app.contact,
        license_info=app.license_info,
        routes=app.routes,
        webhooks=app.webhooks.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )
    headers = schema.setdefault("components", {}).setdefault("headers", {})
    headers["CruciblePrivilegeMode"] = {
        "description": "Privilege mode applied to the authenticated request.",
        "schema": {
            "type": "string",
            "enum": [mode.value for mode in PrivilegeMode],
        },
    }
    headers["CrucibleRequestId"] = {
        "description": "Server-generated request identifier for log correlation.",
        "schema": {"type": "string", "format": "uuid"},
    }
    headers["PrivateCacheControl"] = {
        "description": "Private cache policy selected for the applied privilege mode.",
        "schema": {"type": "string"},
    }
    headers["PrivilegeModeVary"] = {
        "description": "Cache key dimensions for authenticated responses.",
        "schema": {"type": "string"},
    }
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            security_requirements = operation.get("security", [])
            authenticated = any(
                "HTTPBearer" in requirement
                for requirement in security_requirements
            )
            for status, response in operation.get("responses", {}).items():
                if not str(status).startswith("2"):
                    continue
                response_headers = response.setdefault("headers", {})
                response_headers.setdefault(
                    "X-Request-ID",
                    {"$ref": "#/components/headers/CrucibleRequestId"},
                )
                if not authenticated:
                    continue
                response_headers[PRIVILEGE_MODE_HEADER] = {
                    "$ref": "#/components/headers/CruciblePrivilegeMode"
                }
                response_headers["Cache-Control"] = {
                    "$ref": "#/components/headers/PrivateCacheControl"
                }
                response_headers["Vary"] = {
                    "$ref": "#/components/headers/PrivilegeModeVary"
                }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
