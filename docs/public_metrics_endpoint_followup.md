# Public `/metrics` endpoint: follow-up

Status: open, not scheduled (2026-10-14).

## Finding

`GET https://crucible.lbl.gov/api/v3/metrics` (and `/testapi-staging/metrics`) is reachable by anyone without authentication. It returns the Prometheus exposition from `prometheus_fastapi_instrumentator` (about 57 KB): every route template with request counts, latency histograms, response sizes, and status-code classes.

It contains no secrets or user data, but it discloses the full endpoint list, traffic volume per route, and error rates. Common practice is to keep metrics internal.

## Who needs it

Only Google Managed Prometheus, through the PodMonitoring manifests in mf-crucible-deploy. It scrapes each pod directly on its container port (7777 on main, 8889 on staging) every 30 seconds, inside the cluster. Nothing uses the public path through nginx and Cloudflare.

## Options

1. **Block at nginx** (smallest change): in `crucible-nginx/nginx.conf`, return 404 for `location = /api/v3/metrics` and `location = /testapi-staging/metrics` before the API proxy locations. The in-cluster scrape is unaffected. Deploy like the 2026-10-13 nginx change: back up the live file, write it in place, compare host and container hashes, `nginx -t`, reload. Verify that the public URL returns 404 and that the Managed Prometheus targets stay up.
2. **Serve metrics on a separate port** that the load balancers do not expose, by moving `.expose(app)` to a second ASGI app or a sidecar. More robust, but it changes the pod spec and the PodMonitoring port.
3. **Require a token** on `/metrics`. Managed Prometheus would then need credentials, which adds secret handling for little gain over option 1.

Option 1 is recommended when this is scheduled.

## Related

`GET /health/ready` is also public. It returns the API version, git commit, branch, schema revisions, and database latency, and runs one small query per call. It is useful to clients and operators and low risk; it can stay public, or later return the detailed body only to internal callers.
