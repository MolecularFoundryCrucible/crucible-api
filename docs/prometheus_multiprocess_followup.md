# Per-worker Prometheus counters: follow-up

Status: open, not scheduled (2026-10-16).

## Finding

The API runs gunicorn with 4 Uvicorn workers per pod (`Dockerfile`, `--workers 4`). `prometheus_fastapi_instrumentator` (`app/api/main.py`, `Instrumentator(...).instrument(app).expose(app)`) keeps its counters and histograms in each worker's own memory, and `GET /metrics` returns the numbers of whichever worker serves the scrape. Google Managed Prometheus scrapes each pod every 30 seconds (`k8s-conf/pod-monitoring-*.yaml`), so consecutive samples of one series come from different workers. Each drop looks like a counter reset, and `rate()` and `increase()` add phantom increments.

Observed on 2026-10-16: `increase(http_requests_total{handler="/project_graph/..."}[30d])` reported about 140,000 requests, while the request log showed 20 calls in its 3-day retention. All request counts and rates from these metrics are inflated by a large, varying factor.

## What remains usable

- Whether a route was ever called: a series exists only after some worker served the route.
- Conservative last-use dates: phantom increments come only from workers that served the route, so they can make the last call look later, never earlier.
- Rough latency shares (fraction of requests under a bucket), since all buckets of a histogram are inflated together; not exact.

## Fix

Use `prometheus_client` multiprocess mode, which the instrumentator supports:

1. Set `PROMETHEUS_MULTIPROC_DIR` to an empty, writable directory in each pod (an `emptyDir` volume), created fresh at container start.
2. Expose metrics from a `CollectorRegistry` with `multiprocess.MultiProcessCollector`, so a scrape aggregates all workers.
3. Mark exited workers with `multiprocess.mark_process_dead` in a gunicorn `child_exit` hook.
4. Keep the metrics route excluded from instrumentation, and verify after deploy that `rate(http_requests_total[5m])` matches the request log for a quiet route.

Gauges need an explicit multiprocess mode (`livesum`, `max`, and so on) if any are added. Alternatively, run one worker per pod and scale with replicas, which removes the problem without multiprocess mode.

Related: `docs/public_metrics_endpoint_followup.md` (the metrics route is publicly reachable).
