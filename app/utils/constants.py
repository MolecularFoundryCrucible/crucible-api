import os

# these should probably move to configmap
secret_store = os.getenv("GCS_SECRET_STORE", "projects/776258882599/secrets")
gcs_client_id = os.getenv("GCS_CLIENT_ID", "")
rmq_host = os.getenv("RMQ_HOST", "localhost")
rmq_port = int(os.getenv("RMQ_PORT", "5672"))
rmq_user = os.getenv("RMQ_USER", "admin")
