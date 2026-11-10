"""
Set dataset and sample owner_user_id from owner_orcid where that names a user, and list the owners
that name nobody, so they can be assigned by hand. Never changes owner_orcid or a row that already
has owner_user_id, and is safe to run again. Instruments are filled by migration f3a9d1c7b842; their
unmatched owners are listed here too.

Usage (from inside the pod):
    /root/.venv/bin/python migrations/data/20261105_fill_owner_user_id.py --unmatched unmatched_owners.csv
    /root/.venv/bin/python migrations/data/20261105_fill_owner_user_id.py --unmatched unmatched_owners.csv --commit
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import update
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.tables import Dataset, Instrument, Sample, User

BATCH_SIZE = 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fill dataset and sample owner_user_id from owner_orcid. A dry run unless --commit is given.")
    parser.add_argument("--unmatched", required=True, help="CSV file to write the owners that name no user to")
    parser.add_argument("--commit", action="store_true", help="Write owner_user_id; without it nothing is changed")
    args = parser.parse_args(argv)
    unmatched = []
    disagreements = 0
    for model, kind in ((Dataset, "dataset"), (Sample, "sample")):
        filled, missing, disagree = fill(model, kind, args.commit)
        unmatched += missing
        disagreements += disagree
        verb = "filled" if args.commit else "dry run, would fill"
        print(f"{kind}: {verb}: {filled}, owner names no user: {len(missing)}, owner_user_id and owner_orcid disagree: {disagree}")
    instruments = unmatched_instruments()
    unmatched += instruments
    print(f"instrument: owner names no user: {len(instruments)}")
    with open(args.unmatched, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("resource_type", "unique_id", "stored_owner"))
        writer.writerows(unmatched)
    print(f"wrote {len(unmatched)} unmatched owners to {args.unmatched}")
    return 1 if disagreements else 0


def fill(model, kind: str, commit: bool) -> tuple[int, list[tuple[str, str, str]], int]:
    filled, missing, last_id = 0, [], 0
    with Session(get_engine()) as session:
        while True:
            rows = session.exec(
                select(model.id, model.unique_id, model.owner_orcid)
                .where(model.owner_user_id.is_(None), model.owner_orcid.is_not(None), model.id > last_id)
                .order_by(model.id).limit(BATCH_SIZE)
            ).all()
            if not rows:
                break
            last_id = rows[-1][0]
            owners = dict(session.exec(select(User.unique_id, User.id).where(User.unique_id.in_({owner for _, _, owner in rows}))).all())
            by_owner: dict[int, list[int]] = {}
            for row_id, unique_id, owner in rows:
                if owner in owners:
                    by_owner.setdefault(owners[owner], []).append(row_id)
                else:
                    missing.append((kind, unique_id, owner))
            filled += sum(len(ids) for ids in by_owner.values())
            if commit:
                for owner_user_id, ids in by_owner.items():
                    session.execute(update(model).where(model.id.in_(ids), model.owner_user_id.is_(None)).values(owner_user_id=owner_user_id))
                session.commit()
        disagree = len(session.exec(
            select(model.id).join(User, User.id == model.owner_user_id).where(model.owner_orcid.is_not(None), model.owner_orcid != User.unique_id)
        ).all())
    return filled, missing, disagree


def unmatched_instruments() -> list[tuple[str, str, str]]:
    with Session(get_engine()) as session:
        rows = session.exec(select(Instrument.unique_id, Instrument.owner).where(Instrument.owner_user_id.is_(None), Instrument.owner.is_not(None)).order_by(Instrument.id)).all()
    return [("instrument", unique_id, owner) for unique_id, owner in rows]


if __name__ == "__main__":
    sys.exit(main())
