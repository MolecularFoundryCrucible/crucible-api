# Resource identifiers

This page describes the identifiers of records and users, the references that path segments, query parameters and request bodies accept, and the naming of those parameters.

## Identifiers

- Projects, samples, datasets, instruments and service accounts have an MFID: 26 characters of lowercase Crockford Base32. MFIDs are never reused.
- A human user's stable identifier is the ORCID iD for an account created through ORCID sign-in, and an MFID for an account created through password registration or another identity provider. An ORCID linked to an account resolves to that account wherever a user reference is accepted.
- Responses expose the stable identifier as `unique_id`.
- Projects have a `project_id` slug and instruments an `instrument_id` slug: 3 to 25 letters, digits, `_` or `-`, starting with a letter or digit, unique and matched case-insensitively. Responses return the stored casing.
- Users also have a `username` (at most 24 lowercase characters, starting with a letter) and an `email`.
- Sample and dataset names are not identifiers. Samples and datasets are addressed only by MFID.

The forms do not overlap: an email contains `@`, an ORCID matches `NNNN-NNNN-NNNN-NNNN`, an MFID is 26 characters, and slugs and usernames are shorter. A reference is resolved by its shape: a valid MFID is looked up as an MFID, anything else as a slug or username.

## Parameter naming

| Suffix | Accepts | Examples |
|---|---|---|
| `x_id` | The stable identity of a user, or the slug of a project or instrument | `owner_id`, `project_id`, `instrument_id` |
| `x_mfid`, `{mfid}` | The MFID only | `project_mfid`, `instrument_mfid`, `{dataset_mfid}` |
| `x_ref`, `{x_ref}` | Any reference form for that entity, resolved by shape | `member_ref`, `{user_ref}`, `{project_ref}` |
| Bare noun | An expanded object, in responses only | `owner`, `lead`, `members` |

The `_id` suffix does not name an MFID in current fields. Response fields that did so are deprecated and returned alongside their `_mfid` replacement. Some request fields predate this rule and take a user reference under a bare name: `owner` and `new_owner` in request bodies, `project_lead`, and `accessible_to_user`.

## Path segments

| Segment | Accepts |
|---|---|
| `{project_ref}` | Project MFID or `project_id` slug |
| `{instrument_ref}` | Instrument MFID or `instrument_id` slug |
| `{user_ref}` | `unique_id` (ORCID or MFID), linked ORCID, or username. Email still resolves but is deprecated in paths because URLs are written to access logs. |
| `{service_account_ref}` | Service-account MFID or username. A human user returns 404. |
| `{principal}` in `/resources/{mfid}/access/{kind}/{principal}` | A user reference when `kind` is `users`, a project reference when `kind` is `projects` |
| `{dataset_mfid}`, `{sample_mfid}`, `{parent_dataset_mfid}`, `{child_dataset_mfid}`, `{parent_sample_mfid}`, `{child_sample_mfid}`, `{mfid}`, `{entity_mfid}`, `{resource_mfid}`, `{file_mfid}` | MFID only |
| `{file_id}` | File MFID |
| `{group_name}` | Raw access-group name; used only by administrator routes under `/access_groups` and `/users/{user_ref}/access_groups` |

The route resolves a reference once and uses the stable identifier for authorization and queries. An unknown reference returns 404. `GET /users/{user_ref}`, `GET /projects/{project_ref}` and `GET /instruments/{instrument_ref}` add a `Content-Location` header with the canonical path when the request used another form.

```http
GET    /projects/{project_ref}
POST   /projects/{project_ref}/users/{user_ref}
POST   /instruments/{instrument_ref}/service_accounts/{service_account_ref}
PUT    /resources/{mfid}/access/{kind}/{principal}
GET    /project_graph/{project_ref}
GET    /datasets/{dataset_mfid}
```

## Query parameters and bodies

Projects and instruments are named by a pair of optional fields. Either may be sent; when both are sent they must name the same record, otherwise the request returns 422. An unknown slug or MFID returns 404.

| Where | Fields |
|---|---|
| `POST /datasets`, `POST /samples` | `project_id`, `project_mfid` |
| `POST /datasets`, `PUT /datasets/{dataset_mfid}/instrument` | `instrument_id`, `instrument_mfid` |
| `POST /resources/{mfid}/project` | `project_id`, `project_mfid` |
| `GET /datasets`, `GET /samples`, their facet and search routes | `project_id`, `project_mfid` |
| `GET /datasets`, `GET /datasets/facets` | `instrument_id`, `instrument_mfid` |

User references in bodies (`owner`, `new_owner`, `project_lead`) and the exact owner filter `owner_id` accept a `unique_id`, linked ORCID, username or email. The access selectors `accessible_to_user` and `accessible_to_project` accept any form of their entity.

Responses that reference a project or instrument include both the slug and the MFID, for example `project_id` and `project_mfid`, or a nested reference with `unique_id` and the slug.

## Exact lookups by handle

| Lookup | Request | Results |
|---|---|---|
| Project slug | `GET /projects?project_id={slug}` | Zero or one |
| Instrument slug | `GET /instruments?instrument_id={slug}` | Zero or one |
| Username | `GET /users?username={username}` | Zero or one per value |
| Email | `GET /users?email={email}` | Zero or one |
| User stable ID or linked ORCID | `GET /users?unique_id={id}` | Zero or one per value |

Exact user lookups return the same representation as `GET /users/{user_ref}`: the user and platform administrators may receive email, other callers receive `UserPublicRead`. `permissive=true` does not make these filters match substrings. An exact instrument slug lookup searches every status unless `status` is given.

`POST /users/resolve` is deprecated; use the repeatable `unique_id` and `username` filters of `GET /users`. Its body field `orcids` is matched against `unique_id`, so it also accepts service-account MFIDs.

`GET /users/by-username/{username}` was removed in 4.0.0 and returns 410 `route_removed`; use `GET /users/{user_ref}`.

## Naming exceptions

`AssociatedFileRead` names the file MFID `mfid` instead of `unique_id`, and exposes both the internal `dataset_id` and `dataset_mfid`.

## Client guidance

Clients expose explicit keyword arguments (`project_id=`, `project_mfid=`, `instrument_id=`, `instrument_mfid=`) and build the request themselves. Because `{x_ref}` segments accept every form, a client can send whichever identifier it holds without a separate lookup. Store MFIDs for durable references.

See also [Permissions](permissions.md).
