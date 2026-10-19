# File name uniqueness follow-up

`POST /datasets/{dataset_mfid}/files` and multipart initiate reject a second record with an existing filename (409 `filename_exists`). Two gaps remain before the database can enforce it.

## Legacy single-shot upload completion

`POST /datasets/{dataset_mfid}/upload/complete` (`app/api/routes/gcs_storage.py`) still inserts a new `AssociatedFile` on every completion without checking the filename, so a re-upload of the same name adds a duplicate record. This is the likely source of the existing duplicates. The file-replacement design updates the existing record in place for this path.

## Existing duplicates

Production has 52 (dataset, filename) pairs with more than one record (counted 2026-10). A unique index on `associatedfile (dataset_id, filename)` cannot be created until they are resolved: keep one record per pair (usually the newest with a stored object), move ingestion requests and other references to it, and retire the rest. Only then add the index, which also closes the race between two concurrent posts of the same name.

```sql
SELECT d.unique_id, f.filename, count(*)
FROM associatedfile f JOIN dataset d ON d.id = f.dataset_id
GROUP BY 1, 2 HAVING count(*) > 1;
```
