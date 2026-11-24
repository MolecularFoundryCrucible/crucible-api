from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import FeatureDisabled
from app.services.storage_downloads import StorageNotConfigured
from app.storage import StoreUnavailable

NOT_AVAILABLE_HERE = 501


def feature_disabled_response(feature: str) -> dict:
    return {NOT_AVAILABLE_HERE: {"description": f"{feature.replace('_', ' ').capitalize()} is not enabled on this server: `{{\"detail\": {{\"code\": \"feature_disabled\", \"feature\": \"{feature}\", \"message\": ...}}}}`"}}


STORAGE_NOT_CONFIGURED_RESPONSE = {
    NOT_AVAILABLE_HERE: {"description": "File storage is not configured on this server: `{\"detail\": {\"code\": \"storage_not_configured\", \"message\": ...}}`"},
    503: {"description": "The file's store is not available on this server: `{\"detail\": {\"code\": \"store_unavailable\", \"store_id\": ..., \"message\": ...}}`"},
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FeatureDisabled)
    async def feature_disabled(request: Request, error: FeatureDisabled):
        return JSONResponse(status_code=NOT_AVAILABLE_HERE, content={"detail": {"code": "feature_disabled", "feature": error.feature, "message": str(error)}})

    @app.exception_handler(StorageNotConfigured)
    async def storage_not_configured(request: Request, error: StorageNotConfigured):
        return JSONResponse(status_code=NOT_AVAILABLE_HERE, content={"detail": {"code": "storage_not_configured", "message": str(error)}})

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, error: StoreUnavailable):
        return JSONResponse(status_code=503, content={"detail": {"code": "store_unavailable", "store_id": error.store_id, "message": f"The store {error.store_id} holding this file is not available on this server"}})
