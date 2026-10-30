# Metadata search performance

Status: documented 2026-10-20, not scheduled. To be designed together with the broader search direction (including the pgvector embeddings in `ResourceEmbedding` and `docs/archive/resource_embedding_review.md`).

## What the route does

`GET /resources/metadata/search?q=` (`search_resource_metadata_v2` in `app/api/routes/resources.py`) runs Postgres full-text search over `scientific_metadata_new.scientific_metadata` (JSONB, model `ScientificMetadataBinary`), filtered by read permission and pending deletions, ranked with `ts_rank_cd`, and returns the top `limit` (at most 50). Callers: the Hub search page in metadata mode (`searchWorkspaceMetadata`) and nano-crucible `search_metadata()`.

The searchable text is `to_tsvector('english', jsonb_path_query_array(scientific_metadata, '$[*]')::text)`, i.e. the whole document serialized as text, keys included. The GIN index `ix_scientific_metadata_fts` (455 MB) is on that same expression.

## Measurements (production, read-only, 2026-10-20)

345,678 metadata rows; live metadata is about 614 MB compressed, largest document 461 kB.

| Term | Matches | Find matches (index) | Full ranked query |
| --- | --- | --- | --- |
| `xrd` | 10 | 0.06 s | fast |
| `perovskite` | 1,289 | 0.06 s | 5.8 s |
| `temperature` | 25,072 | 0.08 s | about 110 s (extrapolated, not run); unranked first 20 in 1.3 s |

The index is used; finding matches is cheap. The cost is ranking: `ts_rank_cd` needs each matching document's tsvector, which the index does not store, so Postgres de-TOASTs the JSONB, serializes it to text, and rebuilds the tsvector for every match (about 4 to 5 ms each). Time grows with the number of matches, so common words are the worst case and can exceed request timeouts. Because keys are indexed, a field name such as `temperature` matches every document that has that field, which inflates match counts and adds noise.

## Options

- **Stored tsvector.** Add a stored column (generated, or maintained on write) holding the tsvector, index it, and rank from it. Ranking stops rebuilding vectors; common terms should drop well under a second. Needs a migration over all rows; a naive `GENERATED ALWAYS ... STORED` rewrites and locks the table, so backfill in batches instead. Extra storage roughly the size of the current index.
- **Bounded ranking.** Rank only the first N matches (for example 500) or skip ranking above a threshold. No migration; fast in all cases, but results for common terms are less well ordered.
- **Values only.** Build the searchable text from JSON values (and possibly selected fields with weights) instead of the serialized document with keys. Smaller index, less noise. Pairs with the stored column since both rebuild the index.
- **Statement timeout.** Whatever the design, give this query its own timeout so a slow search fails cleanly instead of holding a worker.
- **Direction.** If semantic search over embeddings becomes the primary metadata search, decide which of the above is still worth doing for keyword search.

## Related: TOAST storage

`scientific_metadata_new` totals 66 GB, almost all in its TOAST table, while the live metadata is about 0.6 GB. The TOAST table's last autovacuum was 2026-04-28. This is most likely space left by metadata rewrites (every PATCH rewrites the whole document). Confirming and reclaiming it (`VACUUM FULL` or `pg_repack`, which need a maintenance window or extension) is a production operation and needs explicit authorization.
