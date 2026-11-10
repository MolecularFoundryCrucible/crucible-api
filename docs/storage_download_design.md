# Storage locations and downloads

This page specifies where the stored object of a file record may be located, and how download links and ingestion requests resolve and sign access to it. It applies to records stored by the API (`storage_backend` `"gcs"`, see [Remote file records](remote_file_records.md)).

## Object layouts

A stored record's `storage_path` is `{bucket}/{key}`. Existing records have one of two layouts:

| Layout | Key | `storage_generation` | Written by |
|---|---|---|---|
| Upload | `{dataset_mfid}/_files/{upload_id}` | Recorded | The [upload routes](multipart_upload_api.md); never overwritten |
| Before 4.0.0 | `{dataset_mfid}/{filename}` | Not recorded | The upload routes removed in 4.0.0 |

`storage_generation` is the store's version of the object: a Google Cloud Storage generation, or an S3 version ID or ETag. The segment `_files` is reserved for uploads; code identifies an upload object by this prefix, not by the identifier format. Replacing a file stored in the older layout moves it to the upload layout ([File replacement](file_replace_design.md)).

## Location rule

An object is served, and an ingestion request created for it, only when its `storage_path` satisfies all of the following:

1. The bucket is `UPLOAD_BUCKET`, `MULTIPART_UPLOAD_BUCKET`, or one of `STORAGE_READ_BUCKETS`.
2. The key starts with `{dataset_mfid}/`, where `dataset_mfid` is the dataset that owns the record, and continues after it.
3. The key has no `.` or `..` segment and no leading `/`. Empty segments are allowed, because Google Cloud Storage treats them literally and keys of the older layout contain them.
4. A key under `{dataset_mfid}/_files/` has a recorded `storage_generation`; without one the object is not a published upload.

A failure returns 409 `{"detail": {"code": "storage_location_invalid", "reason": "<reason>", "message": "File storage location is not valid: <reason>"}}`, where `reason` is `unparseable`, `bucket_not_allowed`, `invalid_segments`, `outside_dataset` or `unpublished`. Clients key on `code`, not on the message. Failures are logged with the file and dataset MFIDs only. When no bucket is configured, the routes return 501 `storage_not_configured`.

## Missing storage paths

A stored record without `storage_path`, such as one registered through `POST /datasets/{dataset_mfid}/files` (which refuses a client `storage_path` for stored records), is located on its first download, ingestion request or replacement. The API lists `{dataset_mfid}/` in `UPLOAD_BUCKET`, skipping `{dataset_mfid}/_files/`, and takes `{dataset_mfid}/{filename}` when it exists, otherwise the single key that ends in `/` followed by the filename's basename. The path found is saved on the record before the URL is signed; the location rule then applies. No match returns 404, several matches return 409. Records are located this way only with `STORAGE_BACKEND=gcs`; on `s3` a record without `storage_path` returns 404.

## Download links

| Route | Response |
|---|---|
| `GET /files/{file_id}/download_link` | `{"url": ..., "expires_in": 3600}` |
| `GET /datasets/{dataset_mfid}/download_links` | An object mapping each file MFID to a URL or `null` |

Both routes require read access to the dataset and accept `?disposition=attachment|inline` (any other value returns 422). A URL is valid for one hour. It is signed by the server's storage identity ([Uploads](multipart_upload_api.md#google-cloud-storage)); on Google Cloud Storage, the URL and the metadata lookup are pinned to the recorded generation.

The `disposition` parameter sets the response headers through signed URL overrides:

- `attachment` (default): `Content-Disposition: attachment` with `filename*=UTF-8''<encoded basename>` and an ASCII fallback, both from the record's filename. The stored content type is served unchanged.
- `inline`: `Content-Disposition: inline` with the same filename, and a content type guessed from the record's filename when it is recognized; otherwise the stored type is kept. Script-capable types are always served as `attachment`: `text/html`, `application/xhtml+xml`, `image/svg+xml`, `text/xml`, `application/xml`, `text/xsl`, and any type ending in `+xml`.

A download button omits the parameter (the browser saves the file under the record's name); a filename link uses `?disposition=inline` (the browser displays the file when its type allows). Programmatic clients ignore the header.

## Errors

| Condition | Single download | Bulk download links | Ingestion |
|---|---|---|---|
| Record not stored by the API | `url: null` | `null` | 400 |
| Storage not configured | 501 | 501 | 501 |
| Missing path, no object found | 404 | `null` | 404 |
| Missing path, several candidates | 409 | `null` | 409 |
| Location rule fails | 409 `storage_location_invalid` | `null` | 409 `storage_location_invalid` |
| Object missing | 404 | `null` | Not checked |
| Signing fails | 503 | `null` | Not applicable |

## Changes to stored records

`PATCH /files/{file_id}` validates the resulting record, not only the request body. For a stored record:

- `storage_path` may be omitted, left unchanged, or cleared to `null` (the next download locates it again). A new value returns 422 `storage_path is assigned by the upload API for GCS files`.
- A record with a recorded `storage_generation` cannot have its `storage_path` cleared (422, same message).
- Changing `size` or `sha256_hash` returns 422 `size and sha256_hash are determined by the stored object for GCS files`. Content changes only through an upload or a [replacement](file_replace_design.md).

For every record, a change to `size`, `sha256_hash`, `storage_path` or `storage_backend` clears `sha256_verified_at` and `storage_crc32c`, and a change to `storage_path` or `storage_backend` clears `storage_generation`. Filename and backend changes are covered in [Remote file records](remote_file_records.md#identity-and-filenames).

`DELETE /files/{file_id}` and dataset hard deletion remove database records only; the stored objects remain ([open items](open_items.md#stored-objects-left-behind-by-deletion)).

## Ingestion

`POST /files/{file_id}/ingest` requires the `create` permission on the dataset. It locates a missing path and applies the location rule before it creates the request; a record that fails either creates no request. With `status=requested` (the default), ingestion must be enabled (otherwise 501 `feature_disabled`) and the API publishes:

```json
{"filename": "<bucket>/<key>", "storage_generation": "<generation>", "file_name": "<record filename>",
 "reqid": 1, "dsid": "<dataset MFID>", "ingestion_class": null}
```

`storage_generation` is present only when it is recorded. Consumers take names and types from `file_name`, never from the key, which for an upload is an opaque identifier; an explicit `ingestion_class` may be needed where a consumer infers it from the name. Upload objects are never overwritten, so a consumer may read the object directly. When publishing fails, the request is recorded as `failed` and the route returns 503 `ingestion_unavailable`. `status=not_requested` records the request without publishing it.
