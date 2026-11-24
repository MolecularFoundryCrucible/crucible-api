import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import update
from sqlmodel import Session, select

from app.services.storage_downloads import multipart_prefix
from app.storage import store_config
from app.storage.registry import COMPATIBILITY_STORE_ID
from app.tables import AssociatedFile, Dataset

logger = logging.getLogger(__name__)


@dataclass
class BackfillReport:
    filled: int = 0
    already: int = 0
    ambiguous: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def backfill_locations(engine, stores, commit: bool, batch: int = 500) -> BackfillReport:
    """Give every stored file written before stores were recorded its store, and those without a
    recorded location their storage_path and generation, found in the store "default" by the rule
    downloads used: the exact key {dataset}/{filename}, else the one object whose name ends in the
    file's basename. Rows where that finds several objects or none are reported and left as they
    are. Reads `batch` rows at a time and writes only with commit, each row in its own transaction and
    only if it has not changed since it was read, so a replace finishing meanwhile is never undone."""
    store = store_config(COMPATIBILITY_STORE_ID)
    adapter = stores(COMPATIBILITY_STORE_ID)
    report = BackfillReport()
    last = 0
    while True:
        with Session(engine) as session:
            rows = session.exec(
                select(AssociatedFile, Dataset.unique_id).join(Dataset, Dataset.id == AssociatedFile.dataset_id)
                .where(AssociatedFile.storage_backend == "gcs", AssociatedFile.store_id.is_(None), AssociatedFile.id > last)
                .order_by(AssociatedFile.id).limit(batch)
            ).all()
            if not rows:
                return report
            session.expunge_all()
        for file, dataset_mfid in rows:
            last = file.id
            seen = (file.storage_path, file.storage_generation)
            found = _locate(adapter, store.bucket, file, dataset_mfid, report)
            if found is None:
                continue
            if not commit:
                _count(report, found, seen)
                continue
            with Session(engine) as session:
                written = session.execute(
                    update(AssociatedFile)
                    .where(AssociatedFile.id == file.id, AssociatedFile.store_id.is_(None), AssociatedFile.storage_path.is_not_distinct_from(seen[0]), AssociatedFile.storage_generation.is_not_distinct_from(seen[1]))
                    .values(storage_path=found[0], storage_generation=found[1], store_id=COMPATIBILITY_STORE_ID)
                ).rowcount
                session.commit()
            if written:
                _count(report, found, seen)
            else:
                report.changed.append(file.mfid)


def _count(report, found, seen):
    if found == seen:
        report.already += 1
    else:
        report.filled += 1


def _locate(adapter, bucket, file, dataset_mfid, report):
    """The (storage_path, generation) to record for the file, or None when it cannot be located."""
    path, generation = file.storage_path, file.storage_generation
    if path and (generation is not None or path.partition("/")[0] != bucket):
        return path, generation
    if not path:
        keys = [key for key in adapter.list_keys(bucket, f"{dataset_mfid}/") if not key.startswith(multipart_prefix(dataset_mfid))]
        exact = f"{dataset_mfid}/{file.filename}"
        basename = PurePosixPath(file.filename).name
        matches = [exact] if exact in keys else [key for key in keys if key.endswith(f"/{basename}")]
        if len(matches) > 1:
            report.ambiguous.append(file.mfid)
            return None
        if not matches:
            report.missing.append(file.mfid)
            return None
        path = f"{bucket}/{matches[0]}"
    stored = adapter.head(bucket, path.partition("/")[2])
    if stored is None:
        report.missing.append(file.mfid)
        return None
    logger.info("file_location_found file=%s path=%s generation=%s", file.mfid, path, stored.version)
    return path, stored.version
