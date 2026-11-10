# Crucible API

The server behind Crucible. It stores scientific records (datasets, samples, projects, instruments, files and their metadata), signs people in, and decides who may see and change what. It is a FastAPI service on PostgreSQL with S3-compatible or Google file storage.

The Molecular Foundry runs it at [crucible.lbl.gov](https://crucible.lbl.gov/api/v3/docs). Any lab can run its own.

## Where to go

| I want to | Read |
|---|---|
| Call the API | [Interactive reference](https://crucible.lbl.gov/api/v3/docs); each [release](https://github.com/MolecularFoundryCrucible/crucible-api/releases) carries its OpenAPI document |
| Run my own Crucible | [Install guide](deploy/compose/README.md) |
| Upgrade to 4.0.0 | [Upgrading from 3.3.1](docs/upgrading/3.3-to-4.0.md) |
| Understand how a part works | [Documentation](docs/README.md) |
| See what changed | [Changelog](CHANGELOG.md) |
| Work on the code | Below, and [AGENTS.md](AGENTS.md) |

## Working on the code

You need Python 3.11, [`uv`](https://docs.astral.sh/uv/), and PostgreSQL with the `vector` and `pg_trgm` extensions.

```bash
uv sync
cp .env.example-localdev .env.local    # then fill in the placeholders
DB_CONF_FILE=.env.local uv run python -m app.cli db upgrade
DB_CONF_FILE=.env.local uv run uvicorn app.api.main:app --reload --port 7777
uv run pytest -q
```

Changes reach `main` through a pull request. A merge to `main` builds the image that staging runs; a `vX.Y.Z` tag releases that same image. The Foundry's deployment lives in the mf-crucible-deploy repository.
