# Workers

Workers are the processes that take work from the message broker, such as the ingestion workers. Each one tells the API what it can do, so the API can advertise it and warn when a request has no worker to take it. Dispatch itself is unchanged: the API publishes to RabbitMQ and any worker consuming the queue may take the message.

## Registration

A worker sends `PUT /workers/{worker_id}` at startup and then every `heartbeat_seconds` from the response (150 s today). Its registration is live until `expires_at`, 10 minutes after the last call, so a worker that stops without deregistering drops out on its own. On a clean shutdown it sends `DELETE /workers/{worker_id}`, which succeeds whether or not the registration exists. A worker should keep running, and keep sending its heartbeat, when the API answers an error.

```json
{"kind": "ingestion",
 "capabilities": [{"name": "MrcIngestor", "description": "MRC electron microscopy maps", "file_extensions": [".mrc"]},
                  {"name": "H5Ingestor"}],
 "buckets": ["mf-storage-prod"], "worker_version": "0.4.1", "package_version": "0.2.1"}
```

- `worker_id`: chosen by the worker and stable for one running instance, such as its hostname (the pod name in Kubernetes); 1 to 128 letters, digits, `_`, `.`, `:` or `-`, starting with a letter or digit. A live registration belongs to the account that made it: another account gets 409 `worker_id_taken` until it expires.
- `kind`: `ingestion` today; other kinds, such as postprocessing, are added when their workers register.
- `capabilities`: for `ingestion`, the ingestors the worker runs (crucible-ingestion's selectable ingestors), at most 500, each with a unique `name`. `description` (up to 2000 characters) and `file_extensions` (file name endings with the leading dot, such as `.mrc` or `.tar.gz`, compared without case) are optional and filled in as ingestors declare them. Bad metadata never costs a worker its registration: an extension without its dot gets one, a bad extension or an overlong description is left out, a bad or repeated name drops that capability, and the response lists everything left out in `ignored` (`name`, `field`, `reason`) for the worker to log. Only a malformed request (an unknown `kind`, wrong types, more than 500 capabilities) is refused with 422.
- `buckets`: the buckets the worker can read, at most 50. The API maps them to its stores (`store_ids` in the listings).
- `worker_version` and `package_version`: the worker's version and, for `ingestion`, crucible-ingestion's.

Registration needs a service account, of any platform role; a person's key is refused with 403. The ingestion worker's key still needs the `admin` platform role for the ingestion itself (see [Later](#later) for the narrower design). It is accepted while ingestion is disabled, so turning ingestion off does not need the workers changed. Registrations not renewed for a day are removed when a worker next registers.

## What the API does with it

- `GET /ingestors` lists the names of live ingestion workers' ingestors, sorted. `GET /ingestors/details` adds each one's description and file extensions; with `?file_name=scan.mrc` it returns only the ingestors whose extensions match, as suggestions for that file. When workers describe the same ingestor differently, as in a rollout, the most recently seen one wins. Both are empty when ingestion is disabled or no worker is live.
- `GET /workers` lists the registrations kept, live or not, sorted by `worker_id` (filter with `?kind=ingestion` and `?live=true` or `false`), with their versions and stores. Administrators and support may read it.
- `POST /files/{file_id}/ingest` with `status=requested` still queues the request, and returns `warnings`: `no_live_worker` when no live worker offers the named ingestor (or none is live, when no ingestor is named), and `store_not_read` when no live worker reads the file's bucket. Nothing is recorded on the request.

Worker health is not part of the API's `/health`: alert on an empty `GET /workers?kind=ingestion` in the deployment's monitoring instead.

## Sending work

An ingestion request or postprocessing run is saved first and then published to RabbitMQ as a persistent message, with publisher confirms and the mandatory flag, so the API knows whether RabbitMQ took it. `published_at` records when it did. When RabbitMQ is down, blocks the connection for 10 s, or has no queue for the routing key, the request is still accepted: it stays `requested` with `published_at` null (ingest adds a `queue_deferred` warning), and `python -m app.cli messaging publish-pending`, scheduled every minute, sends it once RabbitMQ takes it. Work still unsent a day after it was made is marked failed. A message may reach a worker twice (for example when a publish succeeds but recording it does not); workers skip a request that is already complete.

## Rollouts

The broker hands a message to any worker on its queue, so during a rollout an old worker may receive a request for an ingestor only the new one has. Such a worker waits about 30 s and requeues the message, and takes the not-supported path when no live registration lists the ingestor.

## Later

These are recorded designs, not implemented.

### Request-scoped tokens instead of an administrator key

The ingestion worker uses an administrator key because an ingestion writes beyond the request's dataset: it creates samples and child datasets on behalf of the uploader, links samples to existing parents, and reads samples in other projects. The least-privilege design follows OAuth token exchange (RFC 8693):

- A service account with an "ingestion worker" capability, set by an administrator, may only register and update ingestion requests.
- When it marks a request `started`, the API issues a short-lived access token for that request that acts as the person who requested the ingestion, records the service account as the actor, and expires when the request ends or after about an hour.
- The worker runs the ingestion with that token, so every call has exactly the uploader's permissions, new records are owned by the uploader, and audit events name both. The worker's key drops its administrator role.

Ingestions the uploader could not do by hand would then fail, such as reading samples in a project they are not in; test those ingestors on staging before removing the administrator role.

### Routing by store

All ingestion workers consume the same three queues (`ingestion-<suffix>-small`, `-medium`, `-large`), which is enough while every worker runs the same image and reads the same buckets. When workers differ, for example one on a lab machine that only reads the `lab-nas` store, the API publishes to a topic exchange with a routing key that names the store, such as `ingestion.<suffix>.<tier>.<store_id>`, and each worker binds its queue only to the patterns it can serve. Registration keeps advertising what the workers offer.
