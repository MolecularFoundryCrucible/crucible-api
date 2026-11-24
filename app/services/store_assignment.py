from sqlalchemy import func, update
from sqlmodel import Session, select

from app.storage import store_config
from app.tables import AssociatedFile, FileVersion, UploadSession

# Rows written before stores were recorded; catalog-only files have no store.
UNASSIGNED = (
    (AssociatedFile, (AssociatedFile.storage_backend == "gcs", AssociatedFile.store_id.is_(None))),
    (FileVersion, (FileVersion.store_id.is_(None),)),
    (UploadSession, (UploadSession.store_id.is_(None),)),
)


def assign_store(engine, store_id: str, commit: bool, batch: int = 10000) -> dict[str, int]:
    """Record store_id on every stored file, file version and upload session that has no store,
    `batch` rows per transaction. Returns the count per table, written or (without commit) found."""
    store_config(store_id)
    counts = {}
    for model, unassigned in UNASSIGNED:
        table = model.__tablename__
        with Session(engine) as session:
            counts[table] = session.exec(select(func.count()).select_from(model).where(*unassigned)).one()
        if not commit:
            continue
        while True:
            with Session(engine) as session:
                ids = session.exec(select(model.id).where(*unassigned).order_by(model.id).limit(batch)).all()
                if not ids:
                    break
                session.execute(update(model).where(model.id.in_(ids), model.store_id.is_(None)).values(store_id=store_id))
                session.commit()
    return counts
