import os

# these should probably move to configmap
secret_store = os.getenv("GCS_SECRET_STORE", "projects/776258882599/secrets")
gcs_client_id = os.getenv("GCS_CLIENT_ID", "776258882599-v17f82atu67g16na3oiq6ga3bnudoqrh.apps.googleusercontent.com")
rmq_host = '34.27.250.61'#os.getenv("RMQ_HOST", "10.128.0.31")
rmq_port = int(os.getenv("RMQ_PORT", "5672"))
rmq_user = os.getenv("RMQ_USER", "admin")
