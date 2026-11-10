# Remote file records

This page specifies how a dataset's file records catalog files that are not stored by the API, such as files on an HPC file system or at a Globus-addressable path, and the identity rules that apply to all file records. For such a file the API records where it is and how to obtain it; it never verifies that the file exists, reads its content or transfers it.

## Fields

| Field | Type | Notes |
|---|---|---|
| `storage_backend` | Free text, default `"gcs"` | `"gcs"` means stored by the API, in whichever store `STORAGE_BACKEND` configures (Google Cloud Storage or S3-compatible); the value is kept for compatibility with existing records and clients. Any other value (`"globus"`, `"nersc-perlmutter"`, `"lawrencium"`) marks a catalog-only record. Values are not validated, so external systems can be distinguished without a schema change. |
| `storage_path` | Nullable text | For a stored record, `{bucket}/{key}`, assigned by the API ([Storage locations and downloads](storage_download_design.md)). For a catalog-only record, a location string such as a Globus URI or a file system path, not interpreted by the API. |
| `access_note` | Nullable text | How to obtain the file when there is no self-service path, for example "request via NERSC allocation X" or "contact the PI for a Globus share". |
| `sha256_hash`, `size` | Required | Required for every backend; supplied by the client. |

File records belong to datasets only ([open items](open_items.md#files-on-samples-and-instruments)). The `file_id` path parameter of `/files/{file_id}` and the `mfid` field of `AssociatedFileRead` hold the file's MFID.

## Identity and filenames

A file's identity in its dataset is its `filename`:

- `POST /datasets/{dataset_mfid}/files` requires the `create` permission. With an existing filename it returns the existing record unchanged when `sha256_hash`, `size` and `storage_backend` match (`storage_path` and `access_note` are neither compared nor updated), and 409 `filename_exists` with the existing `file_mfid` and `etag` otherwise. Identical content under different filenames gets separate records, so a dataset with `freq_001/kpoints.json` and `freq_002/kpoints.json` keeps both. A filename reserved by an upload in progress returns 409.
- Catalog-only filenames must be canonical relative POSIX paths: not empty, no leading, trailing or repeated `/`, no `.` or `..` components, no ASCII control characters (U+0000 to U+001F, U+007F). Invalid names return 422; nothing is normalized. Clients rebuild datasets on disk from these paths.
- For catalog-only records the database enforces both rules: check constraints on the path and a partial unique index on `(dataset_id, filename)`, which also resolves two concurrent requests with the same name.
- `PATCH /files/{file_id}` cannot change a catalog-only filename (409 `filename_immutable`) and cannot move a record between stored and catalog-only (409 `storage_backend_class_change`). A stored file can be renamed under the upload filename rules (422 otherwise) to a filename not used in the dataset (409 `filename_exists` otherwise).

The path rules and the unique index do not apply to stored records created through `POST /datasets/{dataset_mfid}/files`; records from before 4.0.0 include absolute paths and names that start with `./` ([open items](open_items.md#duplicate-file-names-on-gcs-records)). A client that writes stored files to disk by name validates the name itself.

## Behavior of catalog-only records

- `GET /files/{file_id}/download_link` returns `{"url": null, "expires_in": null}`, and `GET /datasets/{dataset_mfid}/download_links` maps the file to `null`. The response shapes are the same as for stored files; clients read `storage_backend` from `GET /files/{file_id}` or `GET /datasets/{dataset_mfid}/files` to know what to expect.
- `POST /files/{file_id}/ingest` returns 400 `Ingestion is not supported for storage_backend='<value>' files.`; ingestion requests are created only for objects the API stores.
- `PATCH /files/{file_id}` may change `storage_path`, `size`, `sha256_hash` and `access_note` freely ([Changes to stored records](storage_download_design.md#changes-to-stored-records) lists the fields this clears).
- Permissions are evaluated as for stored files: reading and downloading require `viewer`, adding a record and requesting ingestion `contributor`, updating `editor`, and deleting `admin` on the dataset ([Permissions](permissions.md)).
