import logging
from fastapi import FastAPI, Request
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.dependencies import get_api_config
from app.api.routes import (auth, files, datasets, gcs_storage, resources,
                            samples, instruments, users, projects, 
                            access_groups, entities, misc, deletion, 
                            ingestion_requests, ai)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(root_path=f"/{get_api_config()['api_path']}")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"{request.method} {request.url.path}")
    return await call_next(request)


Instrumentator().instrument(app).expose(app)

app.include_router(auth.router)
app.include_router(files.router)
app.include_router(ingestion_requests.router)
app.include_router(datasets.router)
app.include_router(gcs_storage.router)
app.include_router(resources.router)
app.include_router(samples.router)
app.include_router(instruments.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(access_groups.router)
app.include_router(entities.router)
app.include_router(misc.router)
app.include_router(deletion.router)
app.include_router(ai.router)
