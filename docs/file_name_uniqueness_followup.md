# File name uniqueness follow-up

`POST /datasets/{dataset_mfid}/files` and multipart initiate reject a second record with an existing filename (409 `filename_exists`). Two gaps remain before the database can enforce it.

## Legacy single-shot upload completion

`POST /datasets/{dataset_mfid}/upload/complete` (`app/api/routes/gcs_storage.py`) still inserts a new `AssociatedFile` on every completion without checking the filename, so a re-upload of the same name adds a duplicate record. This is the likely source of the existing duplicates. The file-replacement design updates the existing record in place for this path.

## Existing duplicates

Production has 52 (dataset, filename) pairs covering 117 file records (analyzed read-only on 2026-10-20). A unique index on `associatedfile (dataset_id, filename)` cannot be created until they are resolved. Decision on 2026-10-20: document, fix later.

| Kind | Pairs | What it means | Fix |
|---|---|---|---|
| Exact duplicates | 29 | Same `sha256_hash` and the same `storage_path`: one stored object, several identical records. | Keep one record per pair (the one ingestion requests reference, else the newest), repoint ingestion requests to it, delete the rest. |
| Overwritten object | 22 | Several records with different hashes and sizes share one `storage_path`. The legacy upload path overwrote the object on each re-upload, so only one record describes the bytes actually stored (for example, 7 records for `api-uploads/sample_note.md` in dataset `0tcpjt9rqsvhh0000e1rp6p5s8`). | Hash the stored object (these files are small), keep the record that matches, repoint references, delete the stale records. |
| No stored object | 1 | Catalog-only records without a `storage_path`. | Keep the newest. |

By project: `MFP10688` (14 records), datasets without a project (10), `crucible-demo` (8), `mosaic_microscope` (5), `MFP10523` (4), `MFP09712` (4), `10k_perovskites` (2), `11k-project` (2), `alphafold_for_microelectronics` (2), `MFP10422` (1). No duplicate record has a `storage_generation`, so none came from the multipart flow.

After the cleanup, a migration adds the unique index, which also closes the race between two concurrent posts of the same name. Deleting records here must not delete stored objects, since the kept record still points at the same object.

```sql
SELECT d.unique_id, f.filename, count(*)
FROM associatedfile f JOIN dataset d ON d.id = f.dataset_id
GROUP BY 1, 2 HAVING count(*) > 1;
```

## Legacy GCS filename shapes

Production GCS records also hold filenames that the non-GCS rules (`remote_file_records.md`) would reject (counted 2026-10-20): 149,514 start with `/`, mostly the original instrument path (`/home/...` 105,526, `/mnt/...` 39,054, `/data/...` 4,928; the stored object usually keeps only the base name), 1,393 contain `.` segments (consumer output such as `./generated_files/...`), and 1 contains `//`. They are descriptive and do not affect downloads, which use `storage_path`. Normalizing them could lose the recorded source path and create new duplicate names, so decide them together with the duplicate cleanup before extending the path rules or the unique index to GCS.
