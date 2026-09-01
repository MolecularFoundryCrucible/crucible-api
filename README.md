# Crucible API

A FastAPI-based REST API for managing scientific datasets, samples, and metadata. The Crucible API provides comprehensive endpoints for data management, authentication, and integration with external services.

## Staging API documentation

- [Swagger UI](https://crucible.lbl.gov/testapi-staging/docs)
- [ReDoc](https://crucible.lbl.gov/testapi-staging/redoc)
- [OpenAPI JSON](https://crucible.lbl.gov/testapi-staging/openapi.json)
- [Changelog](CHANGELOG.md)

## Features

- **Dataset Management**: Create, update, delete, and query scientific datasets
- **Sample Tracking**: Manage samples with hierarchical relationships and metadata
- **ORCID Authentication**: Secure login using ORCID OAuth2 flow
- **API Key Management**: Generate and manage API keys for programmatic access
- **File Management**: Upload, download, and associate files with datasets
- **Metadata Support**: Scientific metadata, thumbnails, and keywords
- **Project Organization**: Group datasets and users into projects
- **Access Control**: Fine-grained permissions with access groups
- **Instrument Tracking**: Manage and track scientific instruments

## Quick Start

### Requirements

- Python 3.11+
- PostgreSQL database
- ORCID OAuth2 application credentials
- Google Cloud Secret Manager (for production)

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd crucible-api
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

OR
```bash
uv sync
```

3. Configure environment variables (or use `.env` file):
```bash
export HOME=/path/to/home
export DB_CONF_FILE=path/to/db_config.yaml
export API_CONF_FILE=path/to/api_config.yaml
```

4. Run the development server:
```bash
uv run uvicorn app.api.main:app --reload --port 7777
```

## Configuration

### Connecting to the Database Locally

download the cloud-sql-proxy

# linux: 
curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.11.0/cloud-sql-proxy.linux.amd64

# mac apple silicon
curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.18.2/cloud-sql-proxy.darwin.arm64
  
chmod +x cloud-sql-proxy

# run it
nohup ./cloud-sql-proxy --address 127.0.0.1 --port 5432 mf-crucible:us-central1:crucible-db --credentials-file <cred-file-path>

# Create a .env file for your python script to read with 
# Create a conda environment with sqlalchemy, sqlmodel, and pydantic


### API Configuration
Create an `api_config.yaml` file:
```yaml
hostname: your-domain.com
api_path: api/v1
orcid_client_id: your_orcid_client_id
```

## Authentication

### ORCID OAuth2 Flow

1. **Login**: `GET /login` - Redirects to ORCID for authentication
2. **Callback**: `GET /auth` - Handles ORCID callback and creates session
3. **API Key**: `GET /user_apikey` - Generate API key for programmatic access

### API Key Authentication

Include the API key in the `Authorization` header:
```
Authorization: Bearer your_api_key_here
```

## API Endpoints

Use the generated [Swagger UI](https://crucible.lbl.gov/testapi-staging/docs), [ReDoc](https://crucible.lbl.gov/testapi-staging/redoc), or [OpenAPI JSON](https://crucible.lbl.gov/testapi-staging/openapi.json) as the authoritative deployed route inventory. The repository checkpoint artifact is [`openapi/crucible-api.json`](openapi/crucible-api.json), its checksum is [`openapi/crucible-api.json.sha256`](openapi/crucible-api.json.sha256), and both are regenerated with `.venv/bin/python -m scripts.export_openapi`. Client owners should start with the [`client migration summary`](docs/client_migration_summary.md). See [`docs/resource_identifier_routes.md`](docs/resource_identifier_routes.md) for the canonical identifier, exact lookup, compatibility, and Python client dispatch contract.

## Data Models

### Core Models
- **Dataset**: Scientific datasets with metadata, files, and relationships
- **Sample**: Physical or digital samples with hierarchical relationships
- **User**: ORCID-authenticated users with permissions
- **Project**: Organizational units for grouping datasets and users
- **AccessGroup**: Permission groups for access control

### Metadata Models
- **ScientificMetadataBinary**: JSONB metadata attached to resources (datasets, samples, etc.)
- **Thumbnail**: Image thumbnails for datasets
- **AssociatedFile**: Files linked to datasets with SHA256 hashing
- **Keyword**: Searchable tags for datasets

## Security Features

- **ORCID OAuth2**: Secure authentication with ORCID
- **JWT Tokens**: Session management with 30-minute expiration
- **API Key Hashing**: HMAC-SHA256 hashed API keys
- **CSRF Protection**: State parameter validation for OAuth flow
- **Rate Limiting**: Prevents abuse of API key generation
- **Secure Cookies**: HttpOnly, Secure, SameSite=strict settings
- **File Integrity**: SHA256 hash verification for uploaded files

## Deployment

### Docker

Build and run with Docker:
```bash
docker build -t crucible-api .
docker run -p 7777:7777 crucible-api
```

### Google Cloud

Deploy using Google Cloud Build:
```bash
gcloud builds submit --config cloudbuild.yaml
```

The application includes:
- Kubernetes deployment configurations in `k8s-conf/`
- Google Cloud Build pipeline in `cloudbuild.yaml`
- Artifact Registry integration

## Development

### Running Tests
```bash
pytest tests/
```

### Code Structure
```
app/
├── api/
│   ├── main.py          # FastAPI application and endpoints
│   └── test.py          # Test endpoints
├── models/
│   ├── core_models.py   # Database models
│   ├── instrument.py    # Instrument models
│   ├── misc.py          # Utility models
│   └── tasks.py         # Background task models
├── utils/
│   ├── auth_utils.py    # Authentication utilities
│   ├── sql_utils.py     # Database utilities
│   ├── rmq_utils.py     # RabbitMQ utilities
│   └── constants.py     # Application constants
└── general_utils.py     # General utility functions
```

## API Response Formats

All endpoints return JSON responses with appropriate HTTP status codes:

- `200 OK` - Successful operation
- `201 Created` - Resource created successfully
- `400 Bad Request` - Invalid request parameters
- `401 Unauthorized` - Authentication required
- `403 Forbidden` - Insufficient permissions
- `404 Not Found` - Resource not found
- `429 Too Many Requests` - Rate limit exceeded
- `500 Internal Server Error` - Server error

## Rate Limiting

API key generation is rate limited to 5 requests per 15-minute window per user to prevent abuse.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Submit a pull request

## License

[Add your license information here]

## Support

For issues and questions, please use the GitHub issue tracker or contact the development team.


# Local Deployment for Testing

Configuration will live in `.env` like this:
** If you would like to test post or patch requests, please connect to the staging database by setting `DB_DATABASE=crucible-staging`
```sh
DB_TYPE=postgres
DB_USERNAME=crucible-api-user
DB_SERVER=127.0.0.1:5432
DB_DATABASE=crucible
PGDB_PW=<SECRET>
DB_AUTH=basic
API_CONF_FILE=app/conf/api_conf_local.yaml
ORCID_CLIENT_ID=APP-E5VUS6XSJS5VFNEN
CLIENT_SECRET=<SECRET>
CRUCIBLE_SECRET_KEY=<SECRET>
CRUCIBLE_API_KEY=<SECRET>
```

To access database in Cloud SQL, we will use google cloud proxy:

```sh
cloud-sql-proxy --port=5432 -c secret/mf-crucible-1e9b3247273f.json mf-crucible:us-central1:crucible-db
```
Note you will need a credential json file from gcp


Start the app:
```sh
uvicorn app.api.main:app --reload --port 7777
```
