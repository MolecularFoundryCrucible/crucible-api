# Kubernetes deployment notes

The API already exposes Prometheus metrics at `/metrics` through `prometheus-fastapi-instrumentator`. The main and staging PodMonitoring manifests in this directory configure collection for their respective deployments.

Enable Google Cloud Managed Service for Prometheus once per cluster if it is not already enabled:

```bash
gcloud container clusters update crucible-cluster \
  --enable-managed-prometheus \
  --region us-central1
```

Apply the manifest for the intended environment:

```bash
kubectl apply -f k8s-conf/pod-monitoring-main.yaml
kubectl apply -f k8s-conf/pod-monitoring-staging.yaml
```

After deployment, verify the pod labels match the selected manifest, the API `/metrics` endpoint responds inside the cluster, and the PodMonitoring resource reports healthy collection. Do not add another instrumentation instance or duplicate dependency setup.
