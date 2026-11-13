# Dataset instrument assignment

This page covers how a dataset's instrument is set at creation, how it is changed afterwards, and how the instrument's name appears in dataset responses. The grants involved are described in [instrument access](instrument_access.md) and [Permissions](permissions.md#instruments-and-service-accounts).

## Assignment at creation

`POST /datasets` accepts `instrument_mfid`, `instrument_id` (the slug), or both; when both are given they must identify the same instrument (422 otherwise). An unknown instrument returns 404, and an instrument pending deletion returns 409 `pending_deletion`. The instrument's operator group receives `contributor` and its members group `viewer` on the new dataset.

Assignment requires no role on the instrument. When a service account creates a dataset for an instrument it is not bound to, the dataset is created and the server logs a warning.

`instrument_name` is not accepted, because instrument names are free text and need not be unique: a request that names an instrument by `instrument_name` returns 422, and a `null` value is ignored.

## Reassignment

```http
PUT /datasets/{dataset_mfid}/instrument
Content-Type: application/json

{"instrument_id": "instrument-slug"}
```

- The body takes `instrument_id`, `instrument_mfid`, or a matching pair; conflicting identifiers return 422 and `instrument_name` is rejected. The same 404 and 409 apply as at creation.
- Requires `admin` on the dataset (`manage_acl`).
- One transaction updates the dataset's instrument, removes the outgoing instrument's operator and members group grants, grants the new operator group `contributor` and members group `viewer`, and updates the dataset's modification time. Other grants are unchanged.
- Resubmitting the current instrument changes nothing except ensuring that its two grants exist.
- The response (`DatasetInstrumentRead`) contains `dataset_mfid`, `instrument`, `previous_instrument` and `previous_instrument_name`.

An instrument cannot be removed from a dataset: there is no `DELETE` route for it.

## Generic update

`PATCH /datasets/{dataset_mfid}` does not change the instrument. Its deprecated `instrument_id` field accepts only the current value, compared case-insensitively, so that clients resubmitting a full record do not fail; any other value, or `null` on a dataset with an instrument, returns 409. `instrument_name` returns 422 unless `null`. Clients omit both fields.

## Display name

Dataset responses read `instrument_id`, `instrument_name` and the nested `instrument` reference from the assigned instrument, so a rename appears in every dataset response. A dataset without an instrument returns `null` for all three.

`GET /datasets` has no `instrument_name` filter (422); filter by `instrument_mfid` or `instrument_id`.
