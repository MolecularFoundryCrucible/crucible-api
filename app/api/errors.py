from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import FeatureDisabled
from app.services.storage_downloads import StorageNotConfigured

NOT_AVAILABLE_HERE = 501


def feature_disabled_response(feature: str) -> dict:
    return {NOT_AVAILABLE_HERE: {"description": f"{feature.replace('_', ' ').capitalize()} is not enabled on this server: `{{\"detail\": {{\"code\": \"feature_disabled\", \"feature\": \"{feature}\", \"message\": ...}}}}`"}}


STORAGE_NOT_CONFIGURED_RESPONSE = {NOT_AVAILABLE_HERE: {"description": "File storage is not configured on this server: `{\"detail\": {\"code\": \"storage_not_configured\", \"message\": ...}}`"}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FeatureDisabled)
    async def feature_disabled(request: Request, error: FeatureDisabled):
        return JSONResponse(status_code=NOT_AVAILABLE_HERE, content={"detail": {"code": "feature_disabled", "feature": error.feature, "message": str(error)}})

    @app.exception_handler(StorageNotConfigured)
    async def storage_not_configured(request: Request, error: StorageNotConfigured):
        return JSONResponse(status_code=NOT_AVAILABLE_HERE, content={"detail": {"code": "storage_not_configured", "message": str(error)}})
