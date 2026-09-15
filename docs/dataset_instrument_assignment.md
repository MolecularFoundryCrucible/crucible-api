# Dataset instrument assignment

## Current contract

Set `instrument_id` when creating a dataset. `instrument_name` remains supported at creation for compatibility, but the instrument slug is the preferred identifier.

Generic `PATCH /datasets/{dataset_mfid}` no longer changes or clears the assigned instrument. The deprecated `instrument_id` and `instrument_name` fields remain in `DatasetUpdate` temporarily so existing clients may resubmit the current value without failing. A different value or removal returns `409 Conflict`.

Clients should stop presenting instrument assignment as editable dataset metadata and omit both fields from PATCH requests.

## Reassignment contract

Instrument assignment and reassignment use a dedicated atomic operation:

```http
PUT /datasets/{dataset_mfid}/instrument
Content-Type: application/json

{"instrument_id": "instrument-slug"}
```

The body accepts `instrument_id` (slug), `instrument_mfid`, or both; supplying both that
identify different instruments is a `422`. `instrument_name` is not accepted — it is a label
derived from the resolved instrument, and letting a caller set it independently is how the
three columns drift apart.

The operation requires `manage_acl` on the dataset. In one transaction it writes
`instrument_id` (the integer FK), `instrument_mfid`, and `instrument_name` from a single
resolved instrument row, removes the outgoing instrument group's grant, grants contributor to
the new instrument group, and updates the resource modification time.

Any transition is allowed: unassigned to assigned, and assigned to a different instrument.
Resubmitting the dataset's current instrument changes no columns but still ensures the ACL row
exists, which repairs datasets whose instrument columns were backfilled without the
accompanying grant.

### Why the outgoing grant is removed narrowly

`resourceacl` records no provenance, so an instrument group's grant cannot be proven to be
instrument-derived rather than deliberately created. Rather than block reassignment on adding
a provenance column, the implementation deletes only the single row belonging to the
*outgoing* instrument's access group, identified from the dataset's current `instrument_mfid`.
Grants to any other group — including other instrument groups — are left alone. This is
strictly narrower than `POST /resources/{mfid}/project`, which deletes every project-group
grant on the resource.

`DELETE /datasets/{dataset_mfid}/instrument` to clear the instrument is not implemented.
