### Deployment notes

##### Grafana and Prometheus set up
1. Enable google cloud prometheus monitoring for the cluster
```
gcloud container clusters update crucible-cluster \
  --enable-managed-prometheus \
  --region us-central1
```
2. Add a podmonitoring service for the deployment
```
kubectl apply -f k8s-conf/pod-monitoring-main.yaml
```
3. Add prometheus monitoring at the fastapi level by adding a /metrics endpoint
```
# main.py
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)
```
4. Update uv env with prometheus
```
uv add prometheus-fastapi-instrumentator
```
5. Rebuild and deploy
