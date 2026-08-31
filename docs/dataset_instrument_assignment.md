# Dataset instrument assignment

## Current contract

Set `instrument_id` when creating a dataset. `instrument_name` remains supported at creation for compatibility, but the instrument slug is the preferred identifier.

Generic `PATCH /datasets/{dataset_mfid}` no longer changes or clears the assigned instrument. The deprecated `instrument_id` and `instrument_name` fields remain in `DatasetUpdate` temporarily so existing clients may resubmit the current value without failing. A different value or removal returns `409 Conflict`.

Clients should stop presenting instrument assignment as editable dataset metadata and omit both fields from PATCH requests.

## Planned reassignment contract

Instrument reassignment should use a dedicated atomic operation:

```http
PUT /datasets/{dataset_mfid}/instrument
Content-Type: application/json

{"instrument_id": "instrument-slug"}
```

```http
DELETE /datasets/{dataset_mfid}/instrument
```

These operations should require `manage_acl`. In one database transaction they must update the dataset instrument references, remove access derived from the previous instrument group, grant contributor access to the new instrument group when applicable, and update the resource modification time. The implementation must distinguish instrument-derived ACLs from explicit grants before reassignment is enabled.

This dedicated operation is not implemented yet.
