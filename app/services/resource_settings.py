from typing import Any

from fastapi import HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.general_utils import get_utc_datetime, if_match_satisfied, merge_patch
from app.models.preferences import serialized_size
from app.models.resource_settings import MAX_SETTINGS_BYTES, RESOURCE_SETTINGS_SCHEMA_VERSION, ResourceSettingsRead, unavailable_default
from app.tables import ResourceSettings


def settings_etag(revision: int) -> str:
    return f'"s{revision}"'


def read_settings(session: Session, mfid: str) -> dict[str, Any]:
    """A record's settings document; empty when it never saved any."""
    row = session.get(ResourceSettings, mfid)
    return dict(row.settings) if row is not None else {}


def settings_response(row: ResourceSettings | None, response: Response) -> ResourceSettingsRead:
    response.headers["ETag"] = settings_etag(row.revision if row else 0)
    if row is None:
        return ResourceSettingsRead(schema_version=RESOURCE_SETTINGS_SCHEMA_VERSION, revision=0, settings={})
    warning = unavailable_default(row.settings.get("default_store_id"), row.mfid)
    return ResourceSettingsRead(schema_version=row.schema_version, revision=row.revision, updated_at=row.updated_at, settings=row.settings, warnings=[warning] if warning else [])


def update_settings(engine, mfid: str, model: type[BaseModel], patch: Any, if_match: str | None) -> ResourceSettings:
    """Merge a JSON Merge Patch into a record's settings, validated by the record type's model, under
    a row lock; 412 when If-Match names another revision. One retry when two first writes race."""
    if not isinstance(patch, dict):
        raise RequestValidationError([{"type": "dict_type", "loc": ("body",), "msg": "Settings patch must be a JSON object", "input": patch}])
    for attempt in range(2):
        with Session(engine) as session:
            row = session.exec(select(ResourceSettings).where(ResourceSettings.mfid == mfid).with_for_update()).one_or_none()
            current = settings_etag(row.revision if row else 0)
            if not if_match_satisfied(if_match, current):
                raise HTTPException(status_code=412, detail={"code": "etag_mismatch", "etag": current, "message": "Settings changed since the given ETag"})
            merged = merge_patch(row.settings if row else {}, patch)
            try:
                model.model_validate(merged)
            except ValidationError as error:
                raise RequestValidationError(error.errors(include_url=False, include_context=False)) from None
            if serialized_size(merged) > MAX_SETTINGS_BYTES:
                raise RequestValidationError([{"type": "too_long", "loc": ("body",), "msg": f"Settings exceed {MAX_SETTINGS_BYTES} bytes", "input": None}])
            if row is None:
                row = ResourceSettings(mfid=mfid, settings=merged, schema_version=RESOURCE_SETTINGS_SCHEMA_VERSION, revision=1, updated_at=get_utc_datetime())
            else:
                row.settings = merged
                row.revision += 1
                row.updated_at = get_utc_datetime()
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                if attempt:
                    raise
                session.rollback()
                continue
            session.refresh(row)
            session.expunge(row)
            return row
