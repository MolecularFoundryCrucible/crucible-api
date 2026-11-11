"""Fill dataset and sample `owner_user_id` from `owner_orcid`, for a database upgraded from 3.x,
where rows written before the column was read have none. Never changes `owner_orcid` or a row that
already has `owner_user_id`, and is safe to run again."""
from sqlalchemy import update
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.tables import Dataset, Instrument, Sample, User

BATCH_SIZE = 1000
KINDS = ((Dataset, "dataset"), (Sample, "sample"))


def fill(model, kind: str, commit: bool) -> tuple[int, list[tuple[str, str, str]], int]:
    """Returns how many rows were (or would be) filled, the owners that name no user as
    (kind, unique_id, stored owner), and how many filled rows disagree with their `owner_orcid`."""
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
