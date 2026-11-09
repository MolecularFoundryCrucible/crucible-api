import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.auth.browser_login import login_session_middleware
from app.api.routes import multipart
from app.build_info import api_version
from app.api.errors import register_error_handlers
from app.config import check_settings, read_secrets
from app.api.dependencies import get_api_root_path
from app.utils.auth_utils import PRIVILEGE_MODE_HEADER
from app.utils.permissions import PrivilegeMode, PrivilegeModeSource
from app.api.routes import (account, api_keys, auth, identities, files, datasets, gcs_storage, resources,
                            samples, instruments, users, projects,
                            access_groups, entities, misc, deletion,
                            ingestion_requests, join_requests, pipelines, service_accounts,
                            resource_graph, hub_auth, oauth, device, passwords, sessions, print as print_routes)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger(__name__)
# Uvicorn's raw query strings include OAuth callback codes; log_requests records safe request metadata.
logging.getLogger("uvicorn.access").disabled = True

api_root_path = get_api_root_path()


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_settings()
    read_secrets()
    yield


app = FastAPI(
    root_path=api_root_path,
    root_path_in_servers=False,
    servers=[{"url": api_root_path}],
    version=api_version(),
    lifespan=lifespan,
)

register_error_handlers(app)
app.add_middleware(login_session_middleware)


HUB_AUTH_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _is_hub_auth_request(request: Request) -> bool:
    path = request.scope["path"]
    root = request.scope.get("root_path", "").rstrip("/")
    if root and path.startswith(root + "/"):
        path = path[len(root):]
    return path == "/auth/hub" or path.startswith(("/auth/hub/", "/oauth/"))


@app.exception_handler(RequestValidationError)
async def handle_request_validation(request: Request, error: RequestValidationError):
    if not _is_hub_auth_request(request):
        return await request_validation_exception_handler(request, error)
    return JSONResponse(
        status_code=422,
        content={"detail": [{
            "loc": ["request"],
            "msg": "Invalid authentication request",
            "type": "value_error",
        }]},
        headers=HUB_AUTH_HEADERS,
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


UNSTORABLE_ERROR_LIMIT = 20


class _NonFiniteNumber:
    def __init__(self, literal):
        self.literal = literal


NUL_REASON = "NUL characters (\\u0000) cannot be stored; remove them before sending"


def _character_reason(code: int):
    if code == 0:
        return NUL_REASON
    if 0xD800 <= code <= 0xDFFF:
        return "unpaired UTF-16 surrogates are not valid Unicode text"
    if 0xFDD0 <= code <= 0xFDEF or (code & 0xFFFE) == 0xFFFE:
        return "Unicode noncharacters are not allowed in JSON text"
    return None


def _unsupported_reason(text: str):
    if text.isascii():
        return NUL_REASON if "\x00" in text else None
    offending = [character for character in set(text) if _character_reason(ord(character))]
    if not offending:
        return None
    return _character_reason(ord(min(offending, key=text.index)))


def _printable(key: str) -> str:
    return "".join(c if _unsupported_reason(c) is None else f"\\u{ord(c):04x}" for c in key)


def _unstorable(value, location=("body",)):
    if isinstance(value, _NonFiniteNumber):
        return [(location, "invalid_number", f"{value.literal} is not valid JSON; send null for a missing value")]
    if isinstance(value, str):
        reason = _unsupported_reason(value)
        return [(location, "unsupported_character", reason)] if reason else []
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = location + (_printable(key),)
            reason = _unsupported_reason(key)
            if reason:
                found.append((child, "unsupported_character", reason))
            found.extend(_unstorable(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_unstorable(item, location + (index,)))
    return found


@app.middleware("http")
async def reject_unstorable_json(request: Request, call_next):
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        body = await request.body()
        if body:
            try:
                problems = _unstorable(json.loads(body, parse_constant=_NonFiniteNumber))
            except ValueError:
                problems = []
            except RecursionError:
                return JSONResponse(status_code=422, content={"detail": [
                    {"type": "json_too_deep", "loc": ["body"], "msg": "JSON nesting is too deep"}
                ]})
            if problems:
                return JSONResponse(status_code=422, content={"detail": [
                    {"type": kind, "loc": list(location), "msg": message}
                    for location, kind, message in problems[:UNSTORABLE_ERROR_LIMIT]
                ]})
    return await call_next(request)


QUIET_ROUTES = {"/health", "/health/live", "/health/ready", "/metrics"}


def _caller(request: Request) -> tuple[str, str]:
    context = getattr(request.state, "authorization_context", None)
    actor = context.actor if context is not None else getattr(request.state, "authenticated_actor", None)
    if actor is None:
        return "-", "-"
    return actor.user_id, actor.credential


def _deprecated_caller_agent(request: Request) -> str:
    if not getattr(request.scope.get("route"), "deprecated", False):
        return ""
    return " user_agent=" + json.dumps(request.headers.get("user-agent", "-")[:200])


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as error:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "%s %s %s %.3fms request_id=%s actor=%s credential=%s%s",
            request.method,
            _route_template(request),
            500,
            duration_ms,
            request.state.request_id,
            *_caller(request),
            _deprecated_caller_agent(request),
        )
        _audit_privileged_request(request, 500, duration_ms)
        if _is_hub_auth_request(request):
            logger.error(
                "Hub authentication failure error_type=%s request_id=%s",
                type(error).__name__, request.state.request_id,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Authentication request failed"},
                headers={**HUB_AUTH_HEADERS, "X-Request-ID": request.state.request_id},
            )
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
    if _is_hub_auth_request(request):
        response.headers.update(HUB_AUTH_HEADERS)
    duration_ms = (time.perf_counter() - started) * 1000
    route = _route_template(request)
    if response.status_code < 400 and route in QUIET_ROUTES:
        return response
    logger.info(
        "%s %s %s %.3fms request_id=%s actor=%s credential=%s%s",
        request.method,
        route,
        response.status_code,
        duration_ms,
        request.state.request_id,
        *_caller(request),
        _deprecated_caller_agent(request),
    )
    _audit_privileged_request(request, response.status_code, duration_ms)
    return response


Instrumentator(excluded_handlers=["^/metrics$", "^/health(/.*)?$"]).instrument(app).expose(app, response_class=PlainTextResponse, responses={200: {"description": "Prometheus metrics in the text exposition format"}})

app.include_router(account.router)
app.include_router(identities.router)
app.include_router(api_keys.router)
app.include_router(auth.router)
app.include_router(hub_auth.router)
app.include_router(hub_auth.link_router)
app.include_router(hub_auth.password_router)
app.include_router(oauth.router)
app.include_router(oauth.discovery)
app.include_router(device.router)
app.include_router(passwords.router)
app.include_router(passwords.pages)
app.include_router(sessions.router)
app.include_router(files.router)
app.include_router(ingestion_requests.router)
app.include_router(pipelines.router)
app.include_router(datasets.router)
app.include_router(gcs_storage.router)
app.include_router(multipart.router)
app.include_router(resources.router)
app.include_router(resource_graph.router)
app.include_router(samples.router)
app.include_router(instruments.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(access_groups.router)
app.include_router(join_requests.router)
app.include_router(entities.router)
app.include_router(misc.router)
app.include_router(deletion.router)
app.include_router(service_accounts.router)
app.include_router(print_routes.router)


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
    for path, path_item in schema.get("paths", {}).items():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            if path.startswith("/auth/hub/"):
                operation["responses"].setdefault("500", {
                    "description": "Authentication request failed; response contains no internal error details",
                })
                if "422" in operation["responses"]:
                    operation["responses"]["422"]["description"] = "Invalid authentication request; submitted values and validation context are omitted"
                for response in operation["responses"].values():
                    response_headers = response.setdefault("headers", {})
                    for name, value in HUB_AUTH_HEADERS.items():
                        response_headers[name] = {"schema": {"type": "string", "const": value}}
                    response_headers["X-Request-ID"] = {"$ref": "#/components/headers/CrucibleRequestId"}
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


@app.middleware("http")
async def multipart_no_store(request: Request, call_next):
    response = await call_next(request)
    if "/upload/multipart/" in request.url.path:
        response.headers["Cache-Control"] = "no-store"
    return response
