"""
Backfill mfid for AssociatedFile rows that have NULL mfid.
Run once inside the API pod after the mfid column was added.

Usage (from inside the pod):
    python migrations/backfill_associatedfile_mfid.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mfid import mfid
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AssociatedFile


def backfill():
    engine = get_engine()
    with Session(engine) as session:
        rows = session.exec(
            select(AssociatedFile).where(AssociatedFile.mfid == None)
        ).all()

        if not rows:
            print("No rows to backfill.")
            return

        print(f"Backfilling {len(rows)} rows...")
        for af in rows:
            af.mfid = mfid()[0]
            session.add(af)

        session.commit()
        print(f"Done. {len(rows)} rows updated.")


if __name__ == "__main__":
    backfill()
