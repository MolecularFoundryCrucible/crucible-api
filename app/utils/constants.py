import os

# these should probably move to configmap
secret_store = os.getenv("GCS_SECRET_STORE", "projects/776258882599/secrets")
gcs_client_id = os.getenv("GCS_CLIENT_ID", "776258882599-v17f82atu67g16na3oiq6ga3bnudoqrh.apps.googleusercontent.com")
rmq_host = os.getenv("RMQ_HOST", "10.128.0.31")
rmq_port = int(os.getenv("RMQ_PORT", "5672"))
rmq_user = os.getenv("RMQ_USER", "admin")

# No default: crucible-label-printer-status is a separately deployed service;
# unset means the printer-status enrichment check is skipped, not an error.
label_printer_status_url = os.getenv("LABEL_PRINTER_STATUS_URL")
