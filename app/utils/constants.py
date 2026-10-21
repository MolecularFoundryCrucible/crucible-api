import os

# these should probably move to configmap
secret_store = os.getenv("GCS_SECRET_STORE", "projects/776258882599/secrets")
rmq_host = os.getenv("RMQ_HOST", "10.128.0.31")
rmq_port = int(os.getenv("RMQ_PORT", "5672"))
rmq_user = os.getenv("RMQ_USER", "admin")
