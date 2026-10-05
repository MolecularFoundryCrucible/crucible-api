# Dataset instrument assignment

## Creation

Set `instrument_mfid` when creating a dataset when the stable identifier is available. `instrument_id` remains supported for slug-based callers. A supplied slug and MFID must resolve to the same instrument.

`instrument_name` is deprecated as a lookup key because instrument names are free-text display and need not be unique. It is consulted only when both `instrument_id` and `instrument_mfid` are omitted. An unregistered name returns 400, and a name matching more than one instrument returns 409.

## Display name

Dataset responses resolve `instrument_name` from the assigned instrument through `instrument_mfid`, matching `instrument_id` and the nested `instrument` reference. Renaming an instrument is reflected in every dataset response. Datasets without a registered instrument return their stored label.

The `instrument_name` filter on `GET /datasets` is deprecated. It matches the same displayed value. Filter by `instrument_mfid` instead.

## Generic update

Generic `PATCH /datasets/{dataset_mfid}` no longer changes or clears the assigned instrument. The deprecated `instrument_id` and `instrument_name` fields remain in `DatasetUpdate` temporarily so existing clients may resubmit the current value without failing. For `instrument_name`, both the stored label and the instrument's current name are accepted. A different value or removal returns `409 Conflict`.

Clients should stop presenting instrument assignment as editable dataset metadata and omit both fields from PATCH requests.

## Assignment and reassignment

Instrument assignment and reassignment use a dedicated atomic operation:

```http
PUT /datasets/{dataset_mfid}/instrument
Content-Type: application/json

{"instrument_id": "instrument-slug"}
```

The body accepts `instrument_id`, `instrument_mfid`, or a matching pair. Conflicting identifiers return 422. `instrument_name` is derived from the resolved instrument and is not accepted.

The operation requires `manage_acl` on the dataset. One transaction updates the instrument references, removes the outgoing instrument group's grant, grants contributor to the new instrument group, and advances the resource modification time.

The route supports initial assignment and reassignment. Resubmitting the current instrument is idempotent and ensures its contributor grant exists. Reassignment removes only the outgoing assigned instrument group's grant; unrelated grants remain unchanged.

`DELETE /datasets/{dataset_mfid}/instrument` to clear the instrument is not implemented.
