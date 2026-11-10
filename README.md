# Crucible API

The server behind the Crucible Data Platform. It is a FastAPI service on PostgreSQL with S3-compatible file storage.

The Molecular Foundry runs it at [crucible.lbl.gov](https://crucible.lbl.gov/api/v3/docs). The service can be self-hosted by any lab or institution.

## Features

- **Data model**: datasets, samples, projects and instruments with persistent identifiers (MFIDs), free-form scientific metadata, and links between records that form a provenance graph.
- **Access control**: role-based permissions granted through projects and access groups, with an explicit elevated mode for administrators.
- **Authentication**: OAuth 2.0 authorization code flow with PKCE and device authorization, backed by OpenID Connect providers such as ORCID or by local passwords, plus named API keys for scripts and instruments.
- **File storage**: multipart uploads and downloads through signed URLs on S3-compatible object storage, with versioned file replacement.
- **Search**: filtering, sorting and pagination on list endpoints, with trigram and full-text search over names and metadata.
- **Operations**: Alembic migrations, an administrative CLI, health and Prometheus metrics endpoints, and a Docker Compose bundle for single-host deployments.
- **Optional integrations**: data ingestion and post-processing over RabbitMQ, label printing over MQTT, and project membership from a proposal database.

## Getting started

### API usage

- [Interactive API reference](https://crucible.lbl.gov/api/v3/docs) of the Molecular Foundry deployment. Every deployment serves its own under `/docs`, and each [release](https://github.com/MolecularFoundryCrucible/crucible-api/releases) includes the OpenAPI document.
- [nano-crucible](https://github.com/MolecularFoundryCrucible/nano-crucible), the Python client and command-line interface.
- [Authentication](docs/sign_in.md) and [permissions](docs/permissions.md).

### Deployment

[`deploy/compose/`](deploy/compose/README.md) provides a Docker Compose bundle that runs the API with PostgreSQL, SeaweedFS and Caddy on a single host. For other environments, build the image from the `Dockerfile` and refer to the [authentication settings](docs/sign_in_operations.md), [storage settings](docs/multipart_upload_api.md) and [database migrations](migrations/README.md).

### Development

Requirements: Python 3.11 or later, [`uv`](https://docs.astral.sh/uv/), and PostgreSQL with the `vector` and `pg_trgm` extensions.

```bash
uv sync
cp .env.example-localdev .env.local    # fill in the placeholders
DB_CONF_FILE=.env.local uv run python -m app.cli db upgrade
DB_CONF_FILE=.env.local uv run uvicorn app.api.main:app --reload --port 7777
```

Run the test suite with `uv run pytest -q`.

## Documentation

| Document | Contents |
|---|---|
| [Documentation index](docs/README.md) | Design and behavior of each part of the API |
| [Installation guide](deploy/compose/README.md) | Self-hosting with Docker Compose |
| [Upgrade guide](docs/upgrading/3.3-to-4.0.md) | Migrating from 3.3.1 to 4.0.0 |
| [Changelog](CHANGELOG.md) | Changes in each release |

## Versioning

Releases follow [Semantic Versioning](https://semver.org/) and are tagged `vX.Y.Z`. Each release is listed in the [changelog](CHANGELOG.md) and published with its OpenAPI document. Releases that require operator action include an upgrade guide in [`docs/upgrading/`](docs/upgrading/). The 4.x series keeps the `/api/v3` path prefix.

## Contributing

Changes are submitted as pull requests against `main`. Run the test suite before opening one, and add a changelog entry under `Unreleased` for any change visible to API users, operators or clients. Repository conventions are in [AGENTS.md](AGENTS.md), with step-by-step workflows for schema changes, routes and releases in [`agent-skills/`](agent-skills/).

## Related projects

- [Crucible Hub](https://github.com/MolecularFoundryCrucible/crucible-hub): web interface.
- [nano-crucible](https://github.com/MolecularFoundryCrucible/nano-crucible): Python client and command-line interface.
- [crucible-ingestion](https://github.com/MolecularFoundryCrucible/crucible-ingestion): ingestion of instrument data.
