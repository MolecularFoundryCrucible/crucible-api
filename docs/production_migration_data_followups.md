# Production Migration Data Follow-ups

Recorded during the production permission migration on 2026-08-31. These accepted housekeeping items do not block the permission cutover.

## Legacy instrument references

The project reference backfill populated `project_mfid` for 125,425 datasets and 10,842 samples, with no dangling project references.

The instrument reference backfill populated `instrument_mfid` for 125,672 datasets by the existing instrument row ID and 10,208 datasets by an exact case-insensitive instrument name. It intentionally left 1,144 datasets unresolved because their legacy labels do not exactly identify a registered instrument.

| Legacy `instrument_name` | Datasets |
|---|---:|
| `insitu_pl` | 1,030 |
| `spinbot_photobox` | 38 |
| `mosaic_microscope` | 33 |
| `MACE` | 17 |
| `MosaicMicroscope` | 16 |
| `nirvana` | 6 |
| `inorganic_xrd` | 1 |
| `Morgan's demo` | 1 |
| `practice thermometer` | 1 |
| `python/pandas` | 1 |
| Total | 1,144 |

These datasets remain readable and retain their original `instrument_name` and resource ACLs. They return a null canonical `instrument_id` and a null embedded `instrument` reference, do not appear in queries scoped by a canonical instrument MFID, and cannot participate reliably in behavior tied to a registered instrument. The final schema permits a null `instrument_mfid`, and current create paths require a registered instrument, so this legacy set should not grow.

Follow-up recipe:

1. Review each legacy label with the relevant instrument owner. Similar strings are candidates for review, not proof of identity.
2. Record an explicit mapping from each accepted legacy label to one registered instrument MFID and slug. Do not use fuzzy matching.
3. Build a guarded data script that reports affected counts in dry-run mode and updates both `dataset.instrument_id` and `dataset.instrument_mfid` in one transaction.
4. Leave labels with no confirmed target unresolved.
5. Verify canonical instrument-filtered dataset reads after each committed mapping.

Use this query to check the remaining grouped state:

```sql
SELECT instrument_name, count(*) AS datasets
FROM dataset
WHERE instrument_mfid IS NULL
  AND (instrument_id IS NOT NULL OR instrument_name IS NOT NULL)
GROUP BY instrument_name
ORDER BY count(*) DESC, instrument_name;
```

The existing `migrations/data/20260811_backfill_instrument_mfid.py` remains safe to rerun, but it only resolves registered row IDs and exact case-insensitive names. It will not apply aliases.

## Legacy ownership anomalies

The initial permission backfill promoted every identity-group ACL to `owner`. The production repair corrected the mapping by keeping the stored canonical owner at `owner` and demoting other legacy writable identity grants to `editor`. No read-only identity grants required demotion.

`migrations/data/20260831_repair_exclusive_resource_owners.py` committed these changes:

| Result | Rows |
|---|---:|
| Noncanonical owner ACLs demoted to `editor` | 972 |
| Missing owner ACLs created for registered owners on valid resources | 5,074 |
| Registered owners whose resource has no `idtype` row | 19 |
| Owner references with no matching identity group | 946 |

The 19 rows without `idtype` and the 946 unresolved owner references remain ownerless. Other ACLs continue to provide access where present. Do not recreate missing `idtype` rows without first checking historical deletion state, and do not create identity groups for unresolved labels without confirming the intended user.

Follow-up recipe:

1. Group the 19 missing-`idtype` rows by resource type and check deletion and audit history before deciding whether the parent row or resource row is authoritative.
2. Group the 946 unresolved references by stored owner value and resource type.
3. Resolve only references that identify an existing or confirmed user. Assign ownership through a guarded data script or the ownership-transfer API after deployment.
4. Leave resources ownerless when no owner can be confirmed. Platform administrators can repair them later.
5. Rerun `migrations/data/20260831_repair_exclusive_resource_owners.py` in dry-run mode and `migrations/data/20260828_audit_permission_cutover_preconditions.py` after any cleanup.

The post-repair production audit reported zero null ACL roles, zero null membership roles, zero duplicate owner ACLs, and zero unclassified groups.
