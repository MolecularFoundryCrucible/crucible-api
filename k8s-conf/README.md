# Kubernetes deployment notes

## Hub authentication cleanup

`hub-auth-cleanup-staging.yaml` and `hub-auth-cleanup-main.yaml` define hourly maintenance jobs at minutes 17 and 37 UTC respectively. Both are suspended by default and are not applied by Cloud Build. They run the cleanup script from the API image with the corresponding API ConfigMap and workload identity, without API listeners or storage mounts. The current ConfigMaps use a private database address, so these jobs need the same private network access as the API, not a local SQL proxy. If database routing changes to localhost, update the jobs too.

Deployment requires approval for each environment. First build an API image containing the cleanup script and apply migration `e92a6b1c730f`. Apply the suspended manifest, then pin its `cleanup` container to the tested SHA-tagged API image using `kubectl set image cronjob/crucible-hub-auth-cleanup-staging cleanup=<tested-image>` (use `main` for production). Do not enable the checked-in `latest` placeholder in production. Reapplying the manifest restores suspension and the placeholder image; repin and reapprove activation afterward. Future API releases must explicitly update this independently deployed job's image.

Create a one-off preview Job from the suspended CronJob with `kubectl create job crucible-hub-auth-cleanup-preview --from=cronjob/crucible-hub-auth-cleanup-staging --dry-run=client -o yaml`. Save and review the output, remove `--commit` from its arguments, then apply that Job after approval. The CLI defaults to one read-only batch even when `--batches` is set. Review counts, database connectivity, and logs. Use a new Job name for subsequent runs. A normal one-off Job created from the CronJob includes `--commit` and deletes expired records, even while the CronJob is suspended.

After a successful preview and an approved one-off committing run, enable the schedule with `kubectl patch cronjob crucible-hub-auth-cleanup-staging --type=merge -p '{"spec":{"suspend":false}}'`. Repeat the rollout separately for main. Suspend it again with the same command using `true`; this stops future scheduling but does not cancel an active Job.

Each run allows ten batches of 1,000 candidates per cleanup category, one retry, and ten minutes total. SQL statements have a 60-second timeout and lock acquisition a 5-second timeout. Removing an expired family also removes its remaining token generations, so the candidate limit is not a strict total-row limit. `Forbid` prevents overlap within one CronJob, not with manually created Jobs or another CronJob; do not launch manual cleanup alongside an active scheduled run. Cleanup is safe to retry after partially committed batches.

Review Job failures and timeouts, time since the last successful run, and count logs. Repeated runs consuming all ten batches with nonzero counts warrant a read-only backlog check and capacity adjustment. Set deployment monitoring to alert on a failed Job or no successful run for over two hours after activation; alerting is not provisioned by these manifests. Logs contain batch counts or an exception class and nonzero exit status on failure, not exception text or database credentials. Keep the existing PostgreSQL autovacuum enabled to reclaim deleted-row space.

The schedule, deadlines, and concurrency policy use native [Kubernetes CronJob controls](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

## API metrics

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
