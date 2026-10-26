"""Audit where associated-file records point before confining storage paths.

Classifies every `associatedfile.storage_path` by backend, bucket, and whether the object key sits under the file's own dataset (`{dataset_mfid}/` or `uploads/{dataset_mfid}/`), under another dataset, or elsewhere. Also counts records that share one storage path. Records outside their own dataset are listed by file and dataset MFID only; filenames, paths, and users are never printed.

Also runs every GCS record through `resolve_storage_object`, the same rule the download and ingestion routes enforce, and reports counts per outcome.

Read-only: the transaction is opened READ ONLY and never committed.

Must be run with the target environment's buckets set in `UPLOAD_BUCKET` and `MULTIPART_UPLOAD_BUCKET`, the same variables the rule reads; otherwise records in an unset bucket are reported as `bucket_not_allowed`, and with neither set the audit stops.

Usage:
    UPLOAD_BUCKET=mf-storage-staging MULTIPART_UPLOAD_BUCKET=mf-crucible-multipart-staging DB_CONF_FILE=.env .venv/bin/python migrations/data/20261013_audit_file_storage_paths.py
    UPLOAD_BUCKET=mf-storage-prod MULTIPART_UPLOAD_BUCKET=<production bucket> DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20261013_audit_file_storage_paths.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.services.storage_downloads import StorageLocationError, resolve_storage_object

CLASSIFIED = """
    WITH parsed AS (
        SELECT af.mfid AS file_mfid,
               d.unique_id AS dataset_mfid,
               coalesce(af.storage_backend, '<null>') AS backend,
               {generation} AS has_generation,
               af.storage_path AS path,
               CASE WHEN position('/' in coalesce(af.storage_path, '')) > 0
                    THEN substr(af.storage_path, position('/' in af.storage_path) + 1) END AS key
        FROM associatedfile af
        JOIN dataset d ON d.id = af.dataset_id
    ), owners AS (
        SELECT p.*,
               CASE WHEN split_part(p.key, '/', 1) = 'uploads' THEN split_part(p.key, '/', 2)
                    ELSE split_part(p.key, '/', 1) END AS key_dataset
        FROM parsed p
    )
    SELECT o.file_mfid, o.dataset_mfid, o.backend, o.has_generation, split_part(o.key, '/', 2) AS key_segment,
           CASE
               WHEN o.path IS NULL OR o.path = '' THEN '<no path>'
               WHEN o.key IS NULL THEN '<no bucket separator>'
               ELSE split_part(o.path, '/', 1)
           END AS bucket,
           CASE
               WHEN o.path IS NULL OR o.path = '' THEN 'no path'
               WHEN o.key IS NULL THEN 'unparseable'
               WHEN o.key ~ '(^|/)\\.\\.?(/|$)' THEN 'dot segment'
               WHEN o.key LIKE '/%' THEN 'leading slash'
               WHEN o.key_dataset = o.dataset_mfid AND split_part(o.key, '/', 1) = 'uploads' THEN 'retired multipart layout'
               WHEN o.key_dataset = o.dataset_mfid AND o.key LIKE '%//%' THEN 'own dataset, empty segment'
               WHEN o.key_dataset = o.dataset_mfid THEN 'own dataset'
               WHEN other.id IS NOT NULL THEN 'another dataset'
               ELSE 'other prefix'
           END AS placement
    FROM owners o
    LEFT JOIN dataset other ON other.unique_id = o.key_dataset AND o.key_dataset <> o.dataset_mfid
"""


def main() -> int:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"connected to : {database!r} on {engine.url.host}:{engine.url.port}")
        print("mode         : READ ONLY")
        total = session.execute(text("SELECT count(*) FROM associatedfile")).scalar_one()
        orphans = session.execute(text(
            "SELECT count(*) FROM associatedfile af LEFT JOIN dataset d ON d.id = af.dataset_id WHERE d.id IS NULL"
        )).scalar_one()
        print(f"file records : {total} ({orphans} without a dataset, excluded below)")
        has_column = session.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'associatedfile' AND column_name = 'storage_generation'"
        )).first() is not None
        classified = CLASSIFIED.format(generation="af.storage_generation IS NOT NULL" if has_column else "false")

        print("\nbackend / bucket / placement / generation -> count")
        rows = session.execute(text(f"""
            SELECT backend, bucket, placement, has_generation, count(*) AS n
            FROM ({classified}) c
            GROUP BY backend, bucket, placement, has_generation
            ORDER BY backend, bucket, placement, has_generation
        """)).all()
        for row in rows:
            print(f"  {row.backend:<8} {row.bucket:<32} {row.placement:<26} gen={'y' if row.has_generation else 'n'}  {row.n}")

        shared = session.execute(text("""
            SELECT count(*) AS paths, coalesce(sum(n), 0) AS records,
                   coalesce(sum(n) FILTER (WHERE datasets > 1), 0) AS cross_dataset_records
            FROM (
                SELECT storage_path, count(*) AS n, count(DISTINCT dataset_id) AS datasets
                FROM associatedfile
                WHERE storage_path IS NOT NULL AND storage_path <> ''
                GROUP BY storage_path
                HAVING count(*) > 1
            ) s
        """)).one()
        print(f"\nshared storage paths : {shared.paths} paths used by {shared.records} records "
              f"({shared.cross_dataset_records} records in paths shared across datasets)")

        reserved = session.execute(text(f"""
            SELECT count(*) FROM ({classified}) c
            WHERE c.key_segment = '_files'
        """)).scalar_one()
        print(f"keys under the reserved {{dataset_mfid}}/_files/ segment : {reserved}")

        flagged = session.execute(text(f"""
            SELECT file_mfid, dataset_mfid, backend, bucket, placement
            FROM ({classified}) c
            WHERE placement IN ('another dataset', 'dot segment', 'leading slash', 'unparseable')
               OR (placement = 'other prefix' AND backend = 'gcs')
            ORDER BY placement, bucket, dataset_mfid
            LIMIT 100
        """)).all()
        print(f"\nflagged gcs records (first 100): {len(flagged)}")
        for row in flagged:
            print(f"  {row.placement:<22} {row.bucket:<32} dataset={row.dataset_mfid} file={row.file_mfid}")
        bucket_env = os.environ.get("MULTIPART_UPLOAD_BUCKET", "").strip()
        print(f"\nMULTIPART_UPLOAD_BUCKET : {bucket_env or '<unset>'}")
        print("location rule outcomes for gcs records (resolve_storage_object):")
        generation_expr = "af.storage_generation" if has_column else "NULL"
        gcs_rows = session.execute(text(f"""
            SELECT af.mfid AS file_mfid, d.unique_id AS dataset_mfid, af.storage_path AS storage_path,
                   {generation_expr} AS storage_generation
            FROM associatedfile af
            JOIN dataset d ON d.id = af.dataset_id
            WHERE af.storage_backend = 'gcs'
        """)).all()

        outcome_counts = {}
        failures = []
        for row in gcs_rows:
            if row.storage_path is None or row.storage_path == "":
                outcome_counts["no path (backfilled on download)"] = outcome_counts.get("no path (backfilled on download)", 0) + 1
                continue
            try:
                resolve_storage_object(row.storage_path, row.storage_generation, row.dataset_mfid)
            except StorageLocationError as error:
                outcome_counts[error.reason] = outcome_counts.get(error.reason, 0) + 1
                if len(failures) < 100:
                    failures.append((error.reason, row.file_mfid, row.dataset_mfid))
            else:
                outcome_counts["ok"] = outcome_counts.get("ok", 0) + 1

        for outcome in sorted(outcome_counts):
            print(f"  {outcome:<30} {outcome_counts[outcome]}")
        print(f"\nfailing gcs records (first 100 of {sum(v for k, v in outcome_counts.items() if k not in ('ok', 'no path (backfilled on download)'))}):")
        for reason, file_mfid, dataset_mfid in failures:
            print(f"  {reason:<22} dataset={dataset_mfid} file={file_mfid}")

        session.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
