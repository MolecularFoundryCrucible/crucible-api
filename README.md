# Crucible API

FastAPI service for Crucible's scientific records: datasets, samples, projects, instruments, metadata, files, identity, and access control.

## API reference

- Production: [Swagger UI](https://crucible.lbl.gov/api/v3/docs), [ReDoc](https://crucible.lbl.gov/api/v3/redoc), [OpenAPI JSON](https://crucible.lbl.gov/api/v3/openapi.json)
- Staging: [Swagger UI](https://crucible.lbl.gov/testapi-staging/docs), [OpenAPI JSON](https://crucible.lbl.gov/testapi-staging/openapi.json)
- Each [GitHub release](https://github.com/MolecularFoundryCrucible/crucible-api/releases) carries its OpenAPI document (`crucible-api.json`) and digest.
- [Documentation index](docs/README.md), [changelog](CHANGELOG.md), [upgrading from 3.3.1 to 4.0.0](docs/upgrading/3.3-to-4.0.md)

The deployed OpenAPI document is authoritative for routes, schemas, parameters, and deprecations. `GET /health/ready` reports the running version, commit, and database revision.

## Local development

You need Python 3.11, [`uv`](https://docs.astral.sh/uv/), and PostgreSQL with the `vector` and `pg_trgm` extensions.

```bash
uv sync
cp .env.example-localdev .env.local    # then fill in the placeholders
DB_CONF_FILE=.env.local uv run python -m app.cli db upgrade
DB_CONF_FILE=.env.local uv run uvicorn app.api.main:app --reload --port 7777
```

`DB_CONF_FILE` names the environment file. `API_PUBLIC_URL` and `API_PATH` in it give the address clients use; the example serves the API under `/testapi`.

```bash
uv run pytest -q                           # PostgreSQL and S3 tests skip unless configured
uv run python -m scripts.export_openapi    # writes openapi/crucible-api.json (untracked)
```

Migrations, the schema baseline, data scripts, and Cloud SQL access are described in [migrations/README.md](migrations/README.md).

## Deployment

- To run your own Crucible on one machine with Docker Compose, follow [deploy/compose/README.md](deploy/compose/README.md).
- This repository builds the image. Each merge to `main` pushes `crucible-api/crucible-api:<commit>` (`cloudbuild.yaml`), and a `vX.Y.Z` tag copies that image into the release repository (`cloudbuild.release.yaml`).
- The Foundry's deployment and runbook live in the mf-crucible-deploy repository (`foundry/RUNBOOK.md`). Staging follows `main`; production changes only through an approved pull request there that promotes a release image.
- `main` is the only long-lived branch. Changes reach it through a reviewed pull request; do not push or force-push to it directly.
