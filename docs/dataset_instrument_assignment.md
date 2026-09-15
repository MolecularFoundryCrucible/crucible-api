# Dataset instrument assignment

## Creation

Set `instrument_mfid` when creating a dataset when the stable identifier is available. `instrument_id` remains supported for slug-based callers, and `instrument_name` remains a compatibility alias. A supplied slug and MFID must resolve to the same instrument.

## Generic update

Generic `PATCH /datasets/{dataset_mfid}` no longer changes or clears the assigned instrument. The deprecated `instrument_id` and `instrument_name` fields remain in `DatasetUpdate` temporarily so existing clients may resubmit the current value without failing. A different value or removal returns `409 Conflict`.

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
