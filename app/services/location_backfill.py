import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath

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


def backfill_locations(engine, stores, commit: bool, batch: int = 500) -> BackfillReport:
    """Give every stored file written before stores were recorded its store, and those without a
    recorded location their storage_path and generation, found in the store "default" by the rule
    downloads used: the exact key {dataset}/{filename}, else the one object whose name ends in the
    file's basename. Rows where that finds several objects or none are reported and left as they
    are. Writes only with commit, `batch` rows per transaction."""
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
            for file, dataset_mfid in rows:
                last = file.id
                _backfill(adapter, store.bucket, file, dataset_mfid, report)
                if commit:
                    session.add(file)
            if commit:
                session.commit()


def _backfill(adapter, bucket, file, dataset_mfid, report):
    path, generation = file.storage_path, file.storage_generation
    if path and (generation is not None or path.partition("/")[0] != bucket):
        file.store_id = COMPATIBILITY_STORE_ID
        report.already += 1
        return
    if not path:
        keys = [key for key in adapter.list_keys(bucket, f"{dataset_mfid}/") if not key.startswith(multipart_prefix(dataset_mfid))]
        exact = f"{dataset_mfid}/{file.filename}"
        basename = PurePosixPath(file.filename).name
        matches = [exact] if exact in keys else [key for key in keys if key.endswith(f"/{basename}")]
        if len(matches) > 1:
            report.ambiguous.append(file.mfid)
            return
        if not matches:
            report.missing.append(file.mfid)
            return
        path = f"{bucket}/{matches[0]}"
    stored = adapter.head(bucket, path.partition("/")[2])
    if stored is None:
        report.missing.append(file.mfid)
        return
    file.storage_path, file.storage_generation, file.store_id = path, stored.version, COMPATIBILITY_STORE_ID
    report.filled += 1
    logger.info("file_location_backfilled file=%s path=%s generation=%s", file.mfid, path, stored.version)
