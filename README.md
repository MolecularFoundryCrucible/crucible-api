# Crucible API

FastAPI service for Crucible scientific datasets, samples, projects, instruments, metadata, files, identity, and access control.

## API documentation

- [Staging Swagger UI](https://crucible.lbl.gov/testapi-staging/docs)
- [Staging ReDoc](https://crucible.lbl.gov/testapi-staging/redoc)
- [Staging OpenAPI JSON](https://crucible.lbl.gov/testapi-staging/openapi.json)
- [Repository OpenAPI checkpoint](openapi/crucible-api.json)
- [Client migration summary](docs/archive/client_migration_summary.md)
- [Documentation index](docs/README.md)
- [Changelog](CHANGELOG.md)

The deployed OpenAPI document is authoritative for routes, request and response schemas, parameters, and deprecations. Check `GET /health/ready` for the deployed commit and database schema revisions before testing a deployment.

## Local development

Requirements:

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL
- Credentials for any cloud services exercised locally

Install the project dependencies:

```bash
uv sync
```

Copy `.env.example-localdev` to an untracked environment file and replace its placeholders. `DB_CONF_FILE` selects that file. `API_CONF_FILE` selects the API YAML configuration, including the configured root path.

Start the API:

```bash
DB_CONF_FILE=.env.local \
API_CONF_FILE=app/conf/api_conf_local.yaml \
uv run uvicorn app.api.main:app --reload --port 7777
```

The local configuration currently declares `/testapi` as its proxy root path. Production uses `/api/v3`; staging uses `/testapi-staging`.

Run tests:

```bash
uv run pytest -q
```

Regenerate the repository OpenAPI checkpoint:

```bash
DB_CONF_FILE=.env.local \
API_CONF_FILE=app/conf/api_conf.yaml \
.venv/bin/python -m scripts.export_openapi
```

## Database access and migrations

For Cloud SQL access, authenticate with Google Application Default Credentials and run the Cloud SQL Auth Proxy against the intended instance on an unused local port:

```bash
cloud-sql-proxy \
  --address 127.0.0.1 \
  --port 5433 \
  <project>:<region>:<instance>
```

Point an untracked environment file at that local port. Confirm the database name and environment before any write. See [migrations/README.md](migrations/README.md) for schema and data migration safeguards and [production migration data follow-ups](docs/production_migration_data_followups.md) for accepted legacy cleanup work.

## Deployment

To run your own Crucible on one machine with Docker Compose, follow [deploy/compose/README.md](deploy/compose/README.md).

This repository builds the image (`Dockerfile`, `cloudbuild.yaml`; release images by `cloudbuild.release.yaml`). The Foundry's deployment configuration and its operations notes (cleanup CronJob, monitoring, autoscaling) live in the mf-crucible-deploy repository.

Changes reach `main`, the only long-lived branch, through a reviewed pull request. Do not push or force-push directly to `main`. Production deploys only from release tags through mf-crucible-deploy.
