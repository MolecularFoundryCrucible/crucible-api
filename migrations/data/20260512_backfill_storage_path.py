"""
Backfill storage_path for AssociatedFile rows where storage_path is NULL.
Checks mf-storage-prod/{dsid}/{basename} for each file.

Usage (from inside the pod):
    /root/.venv/bin/python migrations/data/20260512_backfill_storage_path.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlmodel import Session, select
from app.api.dependencies import get_engine
from app.models.core_models import AssociatedFile, Dataset
from app.api.routes.gcs_storage import _get_gcs_storage_client
from app.services.storage_downloads import upload_bucket

PROGRESS_EVERY = 500


def backfill():
    bucket_name = upload_bucket()
    engine = get_engine()

    with Session(engine) as session:
        rows = session.exec(
            select(AssociatedFile, Dataset)
            .join(Dataset, Dataset.id == AssociatedFile.dataset_id)
            .where(AssociatedFile.storage_path == None)
        ).all()

    if not rows:
        print("No rows to backfill.")
        return

    print(f"Found {len(rows)} files with no storage_path. Listing blobs in {bucket_name}...")

    storage_client = _get_gcs_storage_client()
    bucket = storage_client.bucket(bucket_name)
    existing_blobs = {blob.name for blob in bucket.list_blobs()}
    print(f"Found {len(existing_blobs)} blobs in {bucket_name}.")

    matched = 0
    skipped = 0

    with Session(engine) as session:
        for i, (af, ds) in enumerate(rows):
            basename = af.filename.split("/")[-1]
            blob_name = f"{ds.unique_id}/{basename}"

            if blob_name in existing_blobs:
                # Re-fetch within this session to update
                af_live = session.get(AssociatedFile, af.id)
                af_live.storage_path = f"{bucket_name}/{blob_name}"
                session.add(af_live)
                matched += 1
            else:
                skipped += 1

            if (i + 1) % PROGRESS_EVERY == 0:
                session.commit()
                print(f"  [{i+1}/{len(rows)}] matched so far: {matched}, skipped: {skipped}")

        session.commit()

    print(f"Done. {matched} storage_path values set, {skipped} files not found in production storage.")


if __name__ == "__main__":
    backfill()
