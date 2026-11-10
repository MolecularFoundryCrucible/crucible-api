# Instrument access

This page covers who manages an instrument record and how service accounts bound to an instrument reach its datasets. Roles, grants and the service-account ceiling are defined in [Permissions](permissions.md#instruments-and-service-accounts); "platform administrator" below means an administrator in elevated mode ([explicit privilege mode](explicit_privilege_mode_design.md)).

An instrument has two independent access mechanisms. No role on the instrument grants access to the datasets recorded with it: maintainers read those only through their own ownership, project membership or explicit grants.

| Mechanism | Members | Controls |
|---|---|---|
| Grants on the instrument record | The owner and any maintainers granted a role on the instrument | The instrument's details, lifecycle status, service-account bindings, and further grants |
| Instrument operator group | Bound service accounts only | `contributor` on every dataset assigned to the instrument |

## Maintainers

Maintainers are ordinary grants on the instrument record, managed through the generic resource access routes:

```http
PUT    /resources/{mfid}/access/users/{principal}
DELETE /resources/{mfid}/access/users/{principal}
GET    /resources/{mfid}/access
```

A user `principal` is an ORCID, service-account MFID or username (an email still resolves but is deprecated in paths). `/access/projects/{principal}` takes a project MFID or slug. Managing grants requires `admin` on the instrument, and a caller cannot grant above their own role. Ownership changes only through `POST /resources/{mfid}/transfer_ownership`.

| Role | Edit details | Change status | Bind service accounts | Grant access |
|---|---|---|---|---|
| `editor` | yes | no | no | no |
| `admin` | yes | yes | yes | up to `admin` |
| `owner` | yes | yes | yes | up to `admin` |

Details are edited with `PATCH /instruments/{instrument_ref}`. The status is changed with `POST /instruments/{instrument_ref}/status?status=`, which takes `active`, `maintenance` or `decommissioned`. `instrument_ref` is the instrument MFID or `instrument_id` slug.

## Reading maintainers

`GET /instruments/{instrument_ref}?include_members=true` and the exact slug lookup `GET /instruments?instrument_id={slug}&include_members=true` return `members`, a list of `AccessGrantRead` entries for the users and projects holding a role on the instrument record, ordered from `owner` down. The operator group, public access and system groups are excluded. `include_members=true` on `GET /instruments` without `instrument_id` returns 422.

`members` is returned to callers who hold a role on the instrument record other than through public access, and to platform administrators. Other callers, including bound service accounts and callers who read the instrument only through public access, receive `members: null`. `GET /resources/{mfid}/access`, the full grant listing, is readable by the same callers.

Instrument list and detail responses include `role`, the caller's effective role on the instrument record, or `null`. Bound service accounts receive `null`, because binding is not a role on the record.

## Finding instruments a caller manages

`GET /instruments` and `GET /instruments/search` accept repeatable `affiliation` values:

- `owner`: the caller's identity group holds `owner` on the instrument.
- `maintainer`: the caller's identity group holds `editor` or `admin` on the instrument.

Repeated values combine with OR, so `affiliation=owner&affiliation=maintainer` lists every instrument the caller owns or maintains.

## Operator group

Assigning a dataset to the instrument, at creation or through `PUT /datasets/{dataset_mfid}/instrument`, grants the operator group `contributor` on the dataset (see [dataset instrument assignment](dataset_instrument_assignment.md)).

```http
GET    /instruments/{instrument_ref}/service_accounts
POST   /instruments/{instrument_ref}/service_accounts/{service_account_ref}
DELETE /instruments/{instrument_ref}/service_accounts/{service_account_ref}
```

- `service_account_ref` is the service account's MFID or username; a reference to anything other than a service account returns 404. Unbinding an account that is not bound returns 404.
- Listing is open to the callers who may read `members`; other callers receive 403. Each entry's `capabilities.can_remove` is true only for callers who may unbind it.
- Binding and unbinding require `admin` on the instrument, so the owner and `admin` maintainers manage bindings. Each route returns the updated list.
- A bound service account reaches every dataset assigned to the instrument at `contributor`. If its key is exposed, a platform administrator rotates it with `POST /service_accounts/{service_account_ref}/rotate_key`, or the account is unbound.
- The deprecated `POST /users/{user_ref}/access_groups/{group_name}` rejects human users for instrument groups with 409.
- The operator group, named after the instrument's MFID, holds no grant on the instrument record itself.

## Display names

`instrument_name` is a free-text display name, not an identifier: an instrument is referenced by `unique_id` or `instrument_id`. Dataset responses read `instrument_name` from the assigned instrument, so a rename appears in every dataset response, and a dataset without an instrument returns `null`. Deprecated name-based lookups are described in [dataset instrument assignment](dataset_instrument_assignment.md).

## Deleting an instrument

An instrument can be permanently deleted only when no datasets are assigned to it. Two paths exist:

```http
POST   /resources/{mfid}/delete          # request, admin on the instrument
PATCH  /deletion_requests/{request_id}?status=approved
DELETE /resources/{mfid}?force=true[&confirm=true]
```

`DELETE` requires `owner` on the instrument or a platform administrator. Without `confirm=true` it returns a preview and deletes nothing; with `confirm=true` it executes. Either call returns 409 with the blocking dataset count while datasets are assigned. The preview reports `service_accounts_unbound`, the operator group's bindings, and `removed_grants`, the grants the operator group holds on other resources.

Deletion removes the operator group, its grants, the service-account bindings and the instrument's own records; the service accounts themselves remain. A `DeletionAuditLog` row is written on both paths, and the `instrument_id` slug can be reused afterwards. See [Deletion](database_deletion_contract.md) for the full behavior.
