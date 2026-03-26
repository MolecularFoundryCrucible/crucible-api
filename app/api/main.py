import datetime
import json
import os
import urllib.parse
from pathlib import Path

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.dependencies import get_api_config
from app.api.routes import auth, datasets, samples, instruments, users, projects, access_groups, entities, misc

app = FastAPI(root_path=f"/{get_api_config()['api_path']}")
Instrumentator().instrument(app).expose(app)

app.include_router(auth.router)
app.include_router(datasets.router)
app.include_router(samples.router)
app.include_router(instruments.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(access_groups.router)
app.include_router(entities.router)
app.include_router(misc.router)
