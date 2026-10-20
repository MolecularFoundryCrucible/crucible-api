# Legacy slug columns: follow-up

Status: open, to schedule after migration `9d41c6a2e7f3` and the matching code are deployed to main (2026-10-19).

## State after step 1

The API reads and writes only `dataset.project_mfid`, `sample.project_mfid`, and `dataset.instrument_mfid`, and derives `project_id`, `instrument_id`, and `instrument_name` in responses from the project and instrument rows. Migration `9d41c6a2e7f3` dropped the foreign keys `dataset_project_id_fkey`, `userprojectlink_project_id_fkey`, and `dataset_instrument_id_fkey`, so these columns no longer constrain anything:

| Column or table | Production on 2026-10-19 |
|---|---|
| `dataset.project_id` | Duplicates `project_mfid` on every row; no row has a slug without an MFID. |
| `sample.project_id` | Same as above. |
| `dataset.instrument_id` | Duplicates `instrument_mfid` on every row. |
| `userprojectlink` | 15,591 rows; all but one mirrored in `useraccessgrouplink`, which is the only membership the API reads. |
| `dataset.instrument_name` | Stale copies on 11,120 datasets with a registered instrument; the only instrument information on 1,142 datasets with 10 distinct names and no registered instrument. |

The columns keep their last values: new rows leave them null, renames do not update them, and the API ignores them.

## Step 2

1. Decide what to do with the 1,142 datasets whose only instrument information is `instrument_name` (link to an existing instrument, register a new one, or leave unassigned). The review list was generated on 2026-10-19.
2. After those decisions are applied, a migration drops `dataset.project_id`, `sample.project_id`, `dataset.instrument_id`, `dataset.instrument_name`, and the `userprojectlink` table, with their indexes. It must run only after step 1 is live everywhere, because pods running older code still select these columns.
