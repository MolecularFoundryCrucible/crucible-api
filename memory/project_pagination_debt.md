---
name: project-pagination-debt
description: Several list endpoints return raw lists with no pagination — needs to be addressed before scaling
metadata:
  type: project
---

Several endpoints return flat unpaginated lists and should be wrapped in `PaginatedResponse` in a future pass:

- `GET /samples/{sample_id}/datasets` — no `offset`, no total count
- Likely others (check any route returning a bare `List[...]` without `PaginatedResponse`)

**Why:** Fine at current data volumes, but will become slow and memory-heavy as datasets grow. Clients have no way to page through large result sets.

**How to apply:** When touching these routes or when a user reports slowness/large payloads, prioritize adding `limit`/`offset` + `PaginatedResponse` wrapper. Follow the pattern already used in `GET /users`, `GET /datasets`, `GET /projects`, etc.
