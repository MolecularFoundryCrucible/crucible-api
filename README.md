# Crucible API

The server behind the Crucible Data Platform. It is a FastAPI service on PostgreSQL with S3-compatible file storage.

The Molecular Foundry deploys Crucible at [crucible.lbl.gov](https://crucible.lbl.gov/api/v4/docs). The Crucible ecosystem is open-source and can be self-hosted by any lab or institution.

## Features

- **Data model**: datasets, samples, projects and instruments with persistent identifiers (MFIDs), free-form scientific metadata, and links between records that form a provenance graph.
- **Access control**: role-based permissions granted through projects and access groups.
- **Authentication**: OAuth 2.0 authorization code flow backed by OpenID Connect and API keys for scripts and instruments.
- **File storage**: multipart uploads and downloads through signed URLs on S3-compatible object storage, with versioned file replacement.
- **Search**: filtering, sorting, and search of resources and their metadata.
- **Operations**: Alembic migrations, an administrative CLI, health and Prometheus metrics endpoints, and a published container image.

## Getting started

### API usage

- [Interactive API reference](https://crucible.lbl.gov/api/v4/docs) of the Molecular Foundry deployment.
- [nano-crucible](https://github.com/MolecularFoundryCrucible/nano-crucible), the Python client and CLI.
- [Authentication](docs/sign_in.md) and [permissions](docs/permissions.md).

### Deployment

Each release is published as `ghcr.io/molecularfoundrycrucible/crucible-api:<version>` for amd64 and arm64. [crucible-selfhost](https://github.com/MolecularFoundryCrucible/crucible-selfhost) installs it with PostgreSQL, the file store, the Hub and a reverse proxy. For other environments, refer to the [authentication settings](docs/sign_in_operations.md), [storage settings](docs/uploads.md) and [database migrations](migrations/README.md) to customize your deployment.

### Development

Requirements: Python 3.11 or later, [`uv`](https://docs.astral.sh/uv/), and PostgreSQL with the `vector` and `pg_trgm` extensions.

```bash
uv sync
cp .env.example-localdev .env.local    # fill in the placeholders
DB_CONF_FILE=.env.local uv run python -m app.cli db upgrade
DB_CONF_FILE=.env.local uv run uvicorn app.api.main:app --reload --port 7777
```

## Documentation

| Document | Contents |
|---|---|
| [Documentation index](docs/README.md) | Design and behavior of each part of the API |
| [crucible-selfhost](https://github.com/MolecularFoundryCrucible/crucible-selfhost) | Installing and running your own Crucible |
| [Upgrade guide](docs/upgrading/3.3-to-4.0.md) | Migrating from 3.3.1 to 4.0.0 |
| [Changelog](CHANGELOG.md) | Changes in each release |

## Versioning

Releases follow [Semantic Versioning](https://semver.org/) and are tagged `vX.Y.Z`. Each release is listed in the [changelog](CHANGELOG.md) and published with its OpenAPI document. Releases that require it include an upgrade guide in [`docs/upgrading/`](docs/upgrading/).

## Contributing

Changes are submitted as pull requests against `main`. Run the test suite before opening one, and add a changelog entry under `Unreleased` for any change visible to API users, operators or clients. Agentic documentation relies on [AGENTS.md](AGENTS.md) with references to [`.agents/skills/`](.agents/skills/).

## Crucible ecosystem

The API is one part of the Crucible Data Platform. The other parts each have their own repository:

| Repository | Role |
|---|---|
| [crucible-hub](https://github.com/MolecularFoundryCrucible/crucible-hub) | Web interface for browsing and managing records |
| [nano-crucible](https://github.com/MolecularFoundryCrucible/nano-crucible) | Python client and `crucible` command line |
| [crucible-selfhost](https://github.com/MolecularFoundryCrucible/crucible-selfhost) | Guides and example configurations for running your own Crucible |
| [crucible-ingestion](https://github.com/MolecularFoundryCrucible/crucible-ingestion) | Ingestion of instrument data |
| [crucible-ingestion-worker](https://github.com/MolecularFoundryCrucible/crucible-ingestion-worker) | RabbitMQ consumer that runs ingestion in the cloud |
| [crucible-parsers](https://github.com/MolecularFoundryCrucible/crucible-parsers) | Parsers that extract scientific metadata from instrument files, for nano-crucible |
| [crucible-upload-uis](https://github.com/MolecularFoundryCrucible/crucible-upload-uis) | Upload interfaces for instrument computers |
| [crucible-skills](https://github.com/MolecularFoundryCrucible/crucible-skills) | Skills for using Crucible from AI agents |

## License

BSD 3-Clause. See [LICENSE](LICENSE).
