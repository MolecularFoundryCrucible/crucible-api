# Crucible API

A FastAPI-based REST API for managing scientific datasets, samples, and metadata. The Crucible API provides comprehensive endpoints for data management, authentication, and integration with external services.

## Staging Branch
https://crucible.lbl.gov/testapi-staging/docs

## Features

- **Dataset Management**: Create, update, delete, and query scientific datasets
- **Sample Tracking**: Manage samples with hierarchical relationships and metadata
- **ORCID Authentication**: Secure login using ORCID OAuth2 flow
- **API Key Management**: Generate and manage API keys for programmatic access
- **File Management**: Upload, download, and associate files with datasets
- **Metadata Support**: Scientific metadata, thumbnails, and keywords
- **Project Organization**: Group datasets and users into projects
- **Access Control**: Fine-grained permissions with access groups
- **External Integrations**: Google Drive transfers, SciCat synchronization
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

### Authentication & Users
- `GET /login` - Initiate ORCID login
- `GET /auth` - ORCID callback handler
- `GET /user_apikey` - Generate API key
- `GET /regenerate_apikey` - Regenerate existing API key
- `GET /account` - Get current user account info
- `GET /users` - List all users (admin only)
- `GET /users/{orcid}` - Get user by ORCID
- `POST /users` - Create new user

### Datasets
- `POST /datasets` - Create new dataset
- `GET /datasets` - List datasets with filtering
- `GET /datasets/{dsid}` - Get specific dataset
- `PATCH /datasets/{dsid}` - Update dataset
- `DELETE /datasets/{dsid}` - Delete dataset
- `GET /datasets/{dsid}/download/{file_name}` - Download dataset file
- `POST /datasets/{dsid}/upload` - Upload files to dataset
- `POST /datasets/{dsid}/ingest` - Start ingestion process

### Scientific Metadata
- `GET /datasets/{dsid}/scientific_metadata` - Get dataset metadata
- `POST /datasets/{dsid}/scientific_metadata` - Add metadata
- `PATCH /datasets/{dsid}/scientific_metadata` - Update metadata

### Thumbnails & Files
- `GET /datasets/{dsid}/thumbnails` - Get dataset thumbnails
- `POST /datasets/{dsid}/thumbnails` - Add thumbnail
- `GET /datasets/{dsid}/associated_files` - Get associated files
- `POST /datasets/{dsid}/associated_files` - Add associated file

### Samples
- `POST /samples` - Create new sample
- `GET /samples` - List samples
- `GET /samples/{sample_id}` - Get specific sample
- `POST /datasets/{dsid}/samples/{sample_id}` - Link sample to dataset
- `GET /datasets/{dsid}/samples` - Get dataset samples
- `GET /samples/{sample_id}/datasets` - Get sample datasets
- `POST /samples/{parent_sample_id}/children/{child_sample_id}` - Create sample hierarchy

### Projects & Collections
- `POST /projects` - Create project
- `GET /projects` - List projects
- `GET /projects/{proj_id}` - Get project details
- `POST /dataset_collections` - Create dataset collection
- `GET /dataset_collections` - List collections

### Access Control
- `POST /access_groups` - Create access group
- `GET /access_groups/{group_name}` - Get access group
- `POST /users/{orcid}/access_groups/{group_name}` - Add user to group
- `GET /users/{orcid}/access_groups` - Get user's access groups

### External Integrations
- `POST /datasets/{dsid}/google_drive_transfer` - Transfer to Google Drive
- `POST /datasets/{dsid}/scicat_update` - Sync with SciCat
- `GET /google_drives` - List Google Drive configurations
- `POST /google_drives` - Add Google Drive configuration

### Instruments & Keywords
- `GET /instruments` - List instruments
- `POST /instruments` - Add instrument
- `GET /keywords/{keyword}` - Search keywords
- `POST /datasets/{dsid}/keywords` - Add keyword to dataset

## Data Models

### Core Models
- **Dataset**: Scientific datasets with metadata, files, and relationships
- **Sample**: Physical or digital samples with hierarchical relationships
- **User**: ORCID-authenticated users with permissions
- **Project**: Organizational units for grouping datasets and users
- **AccessGroup**: Permission groups for access control

### Metadata Models
- **ScientificMetadata**: Key-value metadata for datasets
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

