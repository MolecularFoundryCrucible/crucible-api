# Sharing and access data: follow-up

Status: open, not scheduled (2026-10-19). Two parts: a future sharing feature (collections) and production access data to clean up.

## How sharing works today

Every permission combines a `UserAccessGroupLink` (user to group, with a role) and a `ResourceACL` (group to record, with a role); the effective role is the lower of the two along a path, and the best path wins. Each resource type uses one mechanism:

| Resource | Access through |
|---|---|
| Project | Membership in the project group (`/projects/{ref}/users`). The project record holds only its own group at owner. |
| Dataset, sample | The owning project's group at admin (follows `POST /resources/{mfid}/project`), the owner's identity group, the instrument operator group at contributor, and optional grants to users, other projects, and public (`/resources/{mfid}/access/...`). |
| Instrument | Owner and maintainers as record grants; operator service accounts as membership in the instrument group. |

Every grant and membership change follows one rule: you may grant, change, or remove a role at most your own; owner moves only by transfer.

Production on 2026-10-19: records are shared with another project in 50 cases, users hold extra grants on 970 datasets and 54 samples, and no project carries grants beyond its own group.

## Gaps

1. No way to share a set of records as one unit: sharing or revoking N records takes N calls, and nothing records that they form one share.
2. No sharing along lineage: sharing a sample does not share its datasets, nor a dataset its derived datasets.
3. Shares do not follow new records: a record added later is not included.
4. Grants do not expire.
5. Grants record no grantor or time, and there is no "what I have shared, and with whom" view (`project_scope=shared` covers part of "shared with me").

## Collections (proposed)

A collection is a named, shareable set of datasets and samples, possibly from several projects, built on the existing model:

- A collection is an access group. Adding a record gives the collection group a role (default viewer) on that record; only someone with admin on the record may add it, so a collection never exposes more than its contributors control.
- People and projects become members of the collection, with the same membership rule as projects.
- Effective access is the lower of the collection's role on the record and the member's role in the collection, which is what permission checks already compute; the authorization core needs no change.
- Additions: bulk add and remove, add with lineage (children and linked records), optional membership expiry, grantor and time on grants and memberships, collection listings, and "shared with me" / "shared by me" views.
- Sharing with another project becomes adding that project as a member, revocable in one step.

Open questions for the design: whether lineage is live (new children join automatically) or captured once; whether collections replace custom groups; who may create collections; how collections appear in the Hub.

## Production access data to review

Read-only findings on 2026-10-19, resolved on 2026-10-20 with explicit approval:

1. **127 datasets and 1 sample** without their owning project's admin grant. Restored on 105; 4 belonged to a project with an empty `project_id` that the August migration had created from blank legacy slugs (its 1,312 datasets and 3 samples were unassigned and the project deleted through the API, audit logged); 19 were orphaned dataset rows with no resource row, left by a failed RGA/TEY ingestion on 2026-05-20 and re-ingested on 2026-08-17, deleted with their 266 failed ingestion requests and an audit entry each.
2. **Custom groups named after unregistered instruments** (`insitu_pl`, `themisx`, `spinbot_photobox`, `mace`, `mosaicmicroscope`, `nirvana`, `inorganic_xrd`, `practice thermometer`, `python/pandas`): no members, 1,173 grants. Removed after the instrument names were mapped to registered instruments (`docs/legacy_slug_columns_followup.md`).
3. **The custom group `unknown`**: viewer on 32,302 datasets, one member (Morgan Wall). Removed. 13,445 of those datasets had no other grant and no owner, project, or instrument; they are reachable only by platform administrators.
4. **35 custom groups with no grants** (`AUM_DEMO…`, `MFP…`, `test…`, ORCID-named, and others). Removed with their 19 memberships (Morgan Wall, Behzad Rad, Michael Connolly, Peter Ercius).

Also on 2026-10-20: 7,638 datasets linked to an instrument but missing the instrument group's grant were repaired through `PUT /datasets/{dataset_mfid}/instrument`. The four `nsf-summer-school-2026-mkw-*` groups (43 grants) are workshop sharing and were left in place.

## Records without any grant (open)

Read-only counts on 2026-10-20, after the repairs above. Nothing changed; to decide later.

| Records with no `resourceacl` row | Count | Notes |
|---|---|---|
| Datasets | about 180,600 | Created 2024-07 to 2026-06. None has an owner, project, or instrument, and none has the legacy `public` flag. About 167,000 were already grantless before 2026-10-20; 13,445 joined when the `unknown` group was removed. Only platform administrators can read them. Names suggest bulk automated output (for example RGA/TEY logs such as `TF001221_total_outgassing_averaged`). |
| `idtype` rows of type `dataset` with no `dataset` row | 37,537 | Resource rows pointing at nothing; the reverse of the 19 orphaned dataset rows deleted on 2026-10-20. |
| Samples | 242 | Created 2026-02 to 2026-06; includes the 3 samples unassigned from the deleted empty-slug project. |

Options: attribute the datasets (by name pattern, upload path, or ingestion history) to an owner or project, or leave them admin-only; delete the orphaned `idtype` rows after checking deletion history, as was done for the 19 dataset rows.
