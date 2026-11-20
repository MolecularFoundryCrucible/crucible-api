# Instrument access

This page covers who manages an instrument, who reads the datasets recorded with it, and how service accounts bound to an instrument reach those datasets. Roles, grants and the service-account ceiling are defined in [Permissions](permissions.md#instruments-and-service-accounts).

Every authenticated caller can read every instrument record. An instrument has two groups:

| Group | Members | Grant on the instrument | Grant on each assigned dataset |
|---|---|---|---|
| Members (`instrument_member`, named `<instrument MFID>:members`) | People, as `owner`, `admin`, `editor` or `viewer` | `owner` | `viewer` |
| Operator group (`instrument`, named `<instrument MFID>`) | Bound service accounts only | None | `contributor` |

A member's role on the instrument is their membership role. On the instrument's datasets every member reaches `viewer` and never more: editing, sharing or deleting a dataset still requires owning it, a project role or an explicit share.

## Members

| Role | Read the instrument's datasets | Edit details | Change status | Bind service accounts | Manage members | Transfer and delete |
|---|---|---|---|---|---|---|
| `viewer` | yes | no | no | no | no | no |
| `editor` | yes | yes | no | no | no | no |
| `admin` | yes | yes | yes | yes | up to `admin` | no |
| `owner` | yes | yes | yes | yes | up to `admin` | yes |

```http
GET    /instruments/{instrument_ref}/users
POST   /instruments/{instrument_ref}/users/{user_ref}?role=viewer|editor|admin
PATCH  /instruments/{instrument_ref}/users/{user_ref}?role=viewer|editor|admin
DELETE /instruments/{instrument_ref}/users/{user_ref}
```

- `instrument_ref` is the instrument MFID or `instrument_id` slug; `user_ref` is an ORCID, username or MFID.
- Listing is open to members and platform administrators; other callers receive 403. Entries are `AccessGroupMemberRead`, highest role first, with `capabilities` for the caller.
- Adding, changing and removing require `admin` on the instrument, and both the new and the current role must be at most the caller's own. A member may remove themselves.
- `role` defaults to `viewer` when adding. Adding someone already a member at another role returns 409; use `PATCH`.
- `owner` is not accepted (422). The owner changes only through `POST /resources/{mfid}/transfer_ownership`, cannot be removed, and their role cannot be changed (409).
- Service accounts cannot be members (422); they are bound to the operator group.
- The creator of an instrument becomes its owner. A platform administrator may create an instrument for another user, who becomes the owner.

User and project grants on an instrument record through `PUT` and `DELETE /resources/{mfid}/access/users|projects/{principal}` return 409 with code `use_instrument_members`. `GET /resources/{mfid}/access` and the public routes remain available.

## Reading members

`GET /instruments/{instrument_ref}?include_members=true` and the exact slug lookup `GET /instruments?instrument_id={slug}&include_members=true` return `members`, the same `AccessGroupMemberRead` entries as `GET /instruments/{instrument_ref}/users`, highest role first, with `capabilities` for the caller. `include_members=true` on `GET /instruments` without `instrument_id` returns 422.

`members` is returned to members and platform administrators; other callers, including bound service accounts and callers who read the instrument only through public access, receive `members: null`.

Instrument list and detail responses include `role`, the caller's membership role, or `null`. Bound service accounts receive `null`, because binding is not membership.

## Finding instruments

`GET /instruments` and `GET /instruments/search` accept repeatable `affiliation` values, combined with OR:

- `owner`: the caller is the owner.
- `maintainer`: the caller is an `editor` or `admin` member.
- `member`: the caller is a member at any role.

## Datasets read through membership

Assigning a dataset to an instrument, at creation or with `PUT /datasets/{dataset_mfid}/instrument`, grants the members group `viewer` and the operator group `contributor` on it; reassignment moves both grants (see [dataset instrument assignment](dataset_instrument_assignment.md)). Joining an instrument therefore gives read access to all of its datasets, including those recorded before the person joined.

- `GET /datasets`, `GET /datasets/facets`, `GET /datasets/search` and `anchor_mfid` reads accept `affiliation=instrument`, which matches datasets the caller reads as a member of the assigned instrument. `affiliation=member` does not include them. The values combine with OR and with `instrument_mfid`. `GET /samples` refuses `instrument` (422).
- A dataset's grant list (`GET /resources/{mfid}/access`) shows the members group as an entry with `principal_type` `instrument_members`, `permission` `viewer`, that cannot be removed.
- Members read each such dataset's grant list, as project viewers do.

## Operator group

```http
GET    /instruments/{instrument_ref}/service_accounts
POST   /instruments/{instrument_ref}/service_accounts/{service_account_ref}
DELETE /instruments/{instrument_ref}/service_accounts/{service_account_ref}
```

- `service_account_ref` is the service account's MFID or username; a reference to anything other than a service account returns 404. Unbinding an account that is not bound returns 404.
- Listing is open to members and platform administrators; other callers receive 403. Each entry's `capabilities.can_remove` is true only for callers who may unbind it.
- Binding and unbinding require `admin` on the instrument. Each route returns the updated list.
- A bound service account reaches every dataset assigned to the instrument at `contributor`. If its key is exposed, a platform administrator rotates it with `POST /service_accounts/{service_account_ref}/rotate_key`, or the account is unbound.
- The operator group and the members group hold grants only on the datasets assigned to the instrument (and the members group on the instrument record). The legacy group routes refuse both groups (409). `python -m app.cli instruments check` reports any grant that does not match membership and assignment, and `--commit` brings them in line.

## Ownership transfer

`POST /resources/{mfid}/transfer_ownership` on an instrument makes the new owner the owner member; the previous owner stays as an `admin` member, as for projects. A service account cannot own an instrument (422). The owner columns of the instrument are updated in the same step.

## Display names

`instrument_name` is a free-text display name, not an identifier: an instrument is referenced by `unique_id` or `instrument_id`. Dataset responses read `instrument_name` from the assigned instrument, so a rename appears in every dataset response, and a dataset without an instrument returns `null`. Requests never accept it in place of an identifier (see [dataset instrument assignment](dataset_instrument_assignment.md)).

## Deleting an instrument

An instrument can be permanently deleted only when no datasets are assigned to it. Two paths exist:

```http
POST   /resources/{mfid}/delete          # request, admin on the instrument
PATCH  /deletion_requests/{request_id}?status=approved
DELETE /resources/{mfid}?force=true[&confirm=true]
```

`DELETE` requires `owner` on the instrument or a platform administrator. Without `confirm=true` it returns a preview and deletes nothing; with `confirm=true` it executes. Either call returns 409 with the blocking dataset count while datasets are assigned. The preview reports `memberships_removed`, the members, and `service_accounts_unbound`, the operator group's bindings.

Deletion removes both groups, their grants and links, and the instrument's own records; the people and service accounts themselves remain. A `DeletionAuditLog` row is written on both paths, and the `instrument_id` slug can be reused afterwards. See [Deletion](deletion.md) for the full behavior.
