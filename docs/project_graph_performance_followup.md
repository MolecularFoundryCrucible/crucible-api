# `/project_graph` performance: follow-up

Status: open, not scheduled (2026-10-16).

## Finding

`GET /project_graph/{project_ref}` returns every readable, active sample and dataset in a project with their links. In the 3 days of retained logs it was called 20 times, all by one API key, with a median of about 3 s.

Measured on production for the largest projects (`MFP08679`, 27,484 datasets; `10k_perovskites`, 29,562 nodes and 72,558 edges):

| Part | Time |
|---|---|
| Database (5-8 queries, `EXPLAIN ANALYZE`) | 170-260 ms |
| `jsonable_encoder` on the result | about 270 ms |
| `json.dumps` | about 45 ms |
| Response size | about 11 MB |

The rest of the 3 s is building the node and edge dicts and transferring the body. For a median project (31 datasets) the database time is about 2 ms. `dataset.project_mfid` and `sample.project_mfid` are indexed.

## Options

1. Return a `JSONResponse` built directly from the dict, skipping `jsonable_encoder` (about 0.3 s saved on the largest projects).
2. Add an `ETag` (for example from the newest modification time and the node and edge counts) and honor `If-None-Match`, so unchanged projects return 304 without encoding or transfer.
3. Leave as is: the route is rarely called and fast for typical projects.

Option 3 was chosen for now.
