# Unused Cloud SQL proxy sidecar: follow-up

Status: open, not scheduled (2026-10-16).

## Finding

Both API deployments (`k8s-conf/deployment-k8s-conf-main.yaml` and `deployment-k8s-conf-staging.yaml`) run a `cloud-sql-proxy` sidecar (declared under `initContainers` with `restartPolicy: Always`). The API does not use it: the configmaps set `db_server: "172.31.16.10:5432"`, so the API connects directly to Cloud SQL's private IP with the `crucible-api-user` password.

Verified on 2026-10-16:

- The pod socket tables (`/proc/net/tcp`, shared by all containers in a pod) showed every database connection going from the pod IP straight to `172.31.16.10:5432` (14 on main, 4 on staging), the proxy in `LISTEN` on `127.0.0.1:5432` with no connections, and no proxy connections to Cloud SQL (port 3307).
- The proxy logs show startup ("ready for new connections") and nothing after.
- The proxy listens on `127.0.0.1` only, so only containers in the same pod could use it; the other container (gcsfuse) does not talk to Postgres. The Hub auth cleanup CronJob has its own manifest without the proxy.

## History

- 2025-03-25 (`fa404b8`): the API's cloud database config points at the private IP.
- 2026-02-10 (`904b3d2`, `934a720`, `67d7ca0`): the staging Kubernetes config is written with the proxy sidecar; `db_server` is switched to `localhost:5432` ("db connected with proxy") and back to the private IP two hours later ("its not localhost"). The sidecar stays. A likely cause is that the proxy runs with `--auto-iam-authn` while the API logs in with a password user; the reason is not recorded.
- 2026-03 (`f3cc223`): the main deployment is aligned with staging and inherits the sidecar.

## Cost

The cluster is GKE Autopilot, which bills by requested resources. The sidecar requests 1 vCPU and 2 GiB per API pod while using about 1m CPU and 11 MiB, roughly 40 USD a month per pod (main and staging, plus each extra main replica the HPA adds). It also adds an image to keep updated and slightly slows pod startup.

## What the proxy would provide

Routing the API through it would give TLS with verified Cloud SQL certificates, connection authorization through IAM, and optionally password-free IAM database login. The direct connection relies on the private network, the password, and the driver's default `sslmode=prefer` (the instance allows both encrypted and unencrypted connections). Performance is effectively the same either way; the proxy adds an in-pod hop of microseconds and slightly slower new connections, which the connection pool hides.

## Options

1. **Remove the sidecar** (recommended): delete the `initContainers` block from both deployment files. No change to how the API connects; frees the requested resources. CI applies the change on the next deploy of each branch.
2. **Use it**: point `db_server` at `127.0.0.1:5432`, align the proxy login mode with the database user (or move the API to IAM database login), and reduce its requests to about 100m CPU and 128 MiB. A deliberate production change to the database path.
3. **Keep it unused but right-sized**: lower the requests; still maintained for no benefit.
