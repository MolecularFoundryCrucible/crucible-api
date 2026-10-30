# Resource embeddings: status and value review

Summary of a review (2026-10-14) by two Gemini 4 Argon agents, one assessing technical status and one assessing value to researchers, each revising after reading the other. Production facts were checked directly against the database the same day.

## Bottom line

The read side of vector search shipped (the `vectors` branch is fully merged and released in 3.1.0), but the write side does not exist in any repository. Production holds a static, one-off snapshot: 28,494 chunks for 19,902 datasets, all from `nomic-embed-text` (768 dimensions), loaded out of band. New and edited records are never embedded, the index was built on an empty table, and the search route has filtering and recall problems. Nothing user-facing uses it yet. With about two weeks of focused work it could power "similar records" and natural-language search that researchers would actually use.

## What exists

- Table `resource_embedding` (migration `9e4c2a7b6d31`, model `ResourceEmbedding`): `resource_mfid`, `chunk_index`, `chunk_text`, `embedding vector(768)`, `model_name`, with an IVFFlat cosine index (`lists = 100`).
- `GET /embeddings`: cursor-paginated listing with filters, returning chunk text and the stored vector.
- `POST /embeddings/search`: caller supplies a 768-float query vector; returns the top `k` (up to 100) chunk hits, filtered by the caller's read permissions.
- Hard deletion removes a resource's embeddings first (`app/utils/deletion_utils.py`).
- Tests in `tests/test_chunk_search.py` cover permission filtering, pagination and cursor tampering, on SQLite only.

## Status by area

| Area | Status | Finding |
| --- | --- | --- |
| Producer | Missing | No code in crucible-api, crucible-ingestion, ingestion-cloud-consumer, nano-crucible, crucible-hub or the other org repos writes embeddings. The production rows came from an untracked script, contrary to the rule that backfills live in `migrations/data/`. |
| Chunking | Missing | No spec or code for which fields become `chunk_text` or how they are split. Only datasets are embedded; samples, projects and instruments are not. |
| Query encoding | Missing | The API cannot turn text into a vector, so callers must run `nomic-embed-text` themselves. `model_name` is optional on search, so a caller can silently compare vectors from different models. |
| Index | Risky | The IVFFlat index was created before any rows existed, which yields poor clustering unless it was rebuilt after the load. Search probes 10 of 100 lists and then applies the permission filter, so a user who can read only a small share of records often gets fewer than `k` hits, or none, even when matches exist. |
| Schema | Partial | `embedding` is nullable; no unique key on `(resource_mfid, model_name, chunk_index)` (production already has 4 duplicate keys); no `model_name` index; no timestamp or content hash to detect staleness; no `ON DELETE CASCADE` (handled in application code instead). |
| Deletion visibility | Risky | Neither route applies `deletion_filter_for`, so records with a pending deletion request still appear, contrary to `docs/database_deletion_contract.md`. |
| Staleness | Missing | No create or update path refreshes embeddings; every edit leaves stale text and vectors. |
| Results | Partial | Hits are raw chunks: no collapsing per record, no record name or type, no project or type filters (only an explicit list of up to 100 MFIDs). |
| Tests and docs | Partial | No Postgres or pgvector tests; `docs/get_query_contract.md` omits both routes; nano-crucible has no client methods. |

## What it could do for researchers

Ranked after reconciling value with the technical gaps:

1. **Similar records ("more like this")** on sample and dataset pages and in Python. Highest value for effort, because it can compare stored vectors server-side without any query-time encoder, so it works on the 19,902 embedded datasets once the index and filters are fixed.
2. **Natural-language and hybrid search** ("TEM images of gold nanorods after annealing"), fused with the existing keyword metadata search. Highest daily value, but needs the encoder and chunker committed so queries share the stored vector space.
3. **Duplicate warning at creation** ("this sample looks like one you made last week"), scoped to the project. Reuses the encoder on short fields.
4. **Suggested missing links** between new datasets and existing samples, which needs embedding at ingestion time.
5. **Better metadata extraction**: ground the existing `extract_metadata` AI route with similar records from the same project.
6. **Project Q&A assistant** on on-prem CBorg with citations to records, last because it needs fresh, permission-correct, deletion-aware retrieval to be trustworthy.

Notes, if built as proposed in `docs/notes_brainstorm.md`, would become one of the most valuable things to embed: they hold the context instruments do not record.

## Recommended plan

| Step | Scope | Effort |
| --- | --- | --- |
| P0 integrity | Migration: HNSW cosine index (works on any table size and supports iterative scans under filters) or a rebuilt IVFFlat with iterative scan; `NOT NULL` embedding; unique `(resource_mfid, model_name, chunk_index)` after removing the 4 duplicates; `embedded_at` and `content_hash` columns; cascade on delete. Routes: apply the deletion filter and require a pinned default model. Postgres tests. | 2 to 3 days |
| P1 similar records | Join `Resource` for `project_id` and `resource_type` filters, collapse to the best chunk per record, return name and type, add `GET /resources/{mfid}/similar`; `client.resources.similar()` in nano-crucible; a "Similar records" panel in the Hub. | 3 to 4 days |
| P2 pipeline | Versioned chunker (name, description, keywords, scientific metadata) and server-side `nomic-embed-text` encoder in the API; idempotent backfill in `migrations/data/` covering samples too; re-embed asynchronously on create, update and ingestion; accept `query_text` on search. | 5 to 7 days |
| P3 hybrid search | Reciprocal rank fusion with the keyword search, grounded metadata extraction, docs, client and Hub search toggle with staleness badges. | 4 to 5 days |
| P4 assistant | Project-scoped Q&A on CBorg over permission-checked chunks and graph lineage, with citations. | 5 to 8 days |

First milestone suggested by both reviewers: P0 plus P1, with plain-text query support pulled forward if the encoder is easy to host, delivering "similar records" and a semantic search toggle that respects permissions and deletions.

## Open questions

1. Who wrote the production snapshot, with what chunking, and is the script recoverable? It should be committed before anything else builds on those rows.
2. Where should the encoder run: inside the API pod, a small dedicated service, or CBorg (which has to be checked for an embedding endpoint and its model)?
3. Was the IVFFlat index rebuilt after the load? If not, a `REINDEX` alone would improve current results.
4. Should non-dataset records (samples, projects, instruments, and later notes) be embedded, and with which fields?
