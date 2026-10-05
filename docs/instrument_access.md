# Instrument access

An instrument has two independent access mechanisms. Managing an instrument does not grant access to the data recorded with it.

| Mechanism | Who is in it | What it controls |
|---|---|---|
| Instrument record ACL | The owner and any maintainers granted a role on the instrument | The instrument's own details, lifecycle status, service-account bindings, and further grants |
| Instrument operator group | Bound service accounts only | `contributor` on every dataset assigned to the instrument |

## Maintainers

Maintainers are ordinary grants on the instrument record, managed through the generic resource access routes:

```http
PUT    /resources/{instrument_mfid}/access/users/{user_ref}
DELETE /resources/{instrument_mfid}/access/users/{user_ref}
GET    /resources/{instrument_mfid}/access
```

`user_ref` accepts an ORCID, service-account MFID, username, or email. Managing grants requires `admin` on the instrument, and a caller cannot grant above their own role. Ownership changes only through `POST /resources/{instrument_mfid}/transfer_ownership`.

| Role | Edit details | Change status | Bind service accounts | Grant access |
|---|---|---|---|---|
| `editor` | yes | no | no | no |
| `admin` | yes | yes | yes | up to `admin` |
| `owner` | yes | yes | yes | up to `admin` |

No instrument role grants access to datasets assigned to the instrument. Maintainers read those datasets only through their own ownership, project membership, or explicit grants.

## Reading maintainers

`GET /instruments/{instrument_mfid}?include_members=true` and the exact slug lookup `GET /instruments?instrument_id={slug}&include_members=true` return `members`, a list of `AccessGrantRead` entries for the people and projects holding a role on the instrument record, ordered from `owner` down. The operator group, public access, and system groups are excluded.

`members` is returned to any caller who holds a role on the instrument record and to platform administrators. Other callers, including bound service accounts and callers who can read the instrument only through public access, receive `members: null`. Using `include_members=true` on `GET /instruments` without `instrument_id` returns 422.

`GET /resources/{instrument_mfid}/access` remains the full ACL listing and still requires `admin`.

## Finding instruments a caller manages

`GET /instruments` and `GET /instruments/search` accept repeatable `affiliation` values:

- `owner`: the caller's identity holds `owner` on the instrument.
- `maintainer`: the caller's identity holds `editor` or `admin` on the instrument.

Repeated values combine with OR, so `affiliation=owner&affiliation=maintainer` lists every instrument the caller owns or maintains.

## Operator group

Each instrument has one operator group named after its MFID. Assigning a dataset to the instrument grants this group `contributor` on the dataset, at creation or through `PUT /datasets/{dataset_mfid}/instrument`.

```http
GET    /instruments/{instrument_mfid}/service_accounts
POST   /instruments/{instrument_mfid}/service_accounts/{service_account_mfid}
DELETE /instruments/{instrument_mfid}/service_accounts/{service_account_mfid}
```

All three require `admin` on the instrument, so the instrument owner and admin maintainers can manage bindings. Only service accounts can be bound. The deprecated generic `POST /users/{user_id}/access_groups/{group_name}` rejects human users for instrument groups with 409.

A bound service account reaches every dataset assigned to the instrument at `contributor`. If a service account's key is exposed, rotate it through `POST /service_accounts/{unique_id}/rotate_key` (platform administrators only) or unbind it.

The operator group holds no grant on the instrument record itself. Instruments created before this change carried an `editor` grant for their operator group; `migrations/data/20261005_remove_instrument_group_record_grants.py` removes it.

## Display names

`instrument_name` is free-text display, not an identifier. Use `unique_id` or `instrument_id` to refer to an instrument. Dataset responses resolve `instrument_name` from the assigned instrument, so renaming an instrument is reflected immediately. Legacy datasets without a registered instrument keep their stored label. See [dataset instrument assignment](dataset_instrument_assignment.md) for the deprecated name-based lookups.
