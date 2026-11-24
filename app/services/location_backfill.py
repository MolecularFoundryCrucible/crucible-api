import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import update
from sqlmodel import Session, select

from app.services.storage_downloads import multipart_prefix
from app.storage import store_config
from app.tables import AssociatedFile, Dataset

logger = logging.getLogger(__name__)
COLUMNS = (AssociatedFile.id, AssociatedFile.mfid, AssociatedFile.filename, AssociatedFile.storage_path, AssociatedFile.storage_generation, Dataset.unique_id)


@dataclass
class BackfillReport:
    paths: int = 0
    generations: int = 0
    ambiguous: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def backfill_locations(engine, stores, store_id: str, commit: bool, generations: bool = False, batch: int = 500, workers: int = 8) -> BackfillReport:
    """Record where the store's files from before multipart uploads are.

    Pass 1, files without a storage_path: the exact key {dataset}/{filename}, else the one object
    whose name ends in the file's basename (the rule downloads used), each dataset listed once; the
    path and the object's generation are recorded. With generations, pass 2, files with a path in
    the store's bucket but no generation: the object's generation is recorded, so cleanup may delete
    it once the file is replaced or deleted. Files where several objects or none match are reported
    and left. Writes only with commit, each row only if it has not changed since it was read."""
    store = store_config(store_id)
    adapter = stores(store_id)
    report = BackfillReport()
    listings: dict[str, list[str]] = {}

    def locate(row):
        file_id, mfid, filename, path, generation, dataset_mfid = row
        if path is None:
            if dataset_mfid not in listings:
                listings[dataset_mfid] = [key for key in adapter.list_keys(store.bucket, f"{dataset_mfid}/") if not key.startswith(multipart_prefix(dataset_mfid))]
            keys = listings[dataset_mfid]
            exact = f"{dataset_mfid}/{filename}"
            basename = PurePosixPath(filename).name
            matches = [exact] if exact in keys else [key for key in keys if key.endswith(f"/{basename}")]
            if len(matches) > 1:
                report.ambiguous.append(mfid)
                return None
            if not matches:
                report.missing.append(mfid)
                return None
            path = f"{store.bucket}/{matches[0]}"
        stored = adapter.head(store.bucket, path.partition("/")[2])
        if stored is None:
            report.missing.append(mfid)
            return None
        return path, stored.version

    def write(row, found):
        file_id, mfid, _, path, generation, _ = row
        if not commit:
            return True
        with Session(engine) as session:
            written = session.execute(
                update(AssociatedFile)
                .where(AssociatedFile.id == file_id, AssociatedFile.storage_path.is_not_distinct_from(path), AssociatedFile.storage_generation.is_not_distinct_from(generation))
                .values(storage_path=found[0], storage_generation=found[1])
            ).rowcount
            session.commit()
        if not written:
            report.changed.append(mfid)
        return bool(written)

    def run(where, count, parallel):
        last = 0
        while True:
            with Session(engine) as session:
                rows = session.exec(
                    select(*COLUMNS).join(Dataset, Dataset.id == AssociatedFile.dataset_id)
                    .where(AssociatedFile.storage_backend == "gcs", AssociatedFile.store_id == store_id, *where, AssociatedFile.id > last)
                    .order_by(AssociatedFile.id).limit(batch)
                ).all()
            if not rows:
                return
            last = rows[-1][0]
            if parallel:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    found = list(pool.map(locate, rows))
            else:
                found = [locate(row) for row in rows]
            for row, location in zip(rows, found):
                if location is not None and write(row, location):
                    count()
                    logger.info("file_location_recorded file=%s path=%s generation=%s", row[1], location[0], location[1])

    run((AssociatedFile.storage_path.is_(None),), lambda: setattr(report, "paths", report.paths + 1), parallel=False)
    if generations:
        run((AssociatedFile.storage_path.startswith(f"{store.bucket}/", autoescape=True), AssociatedFile.storage_generation.is_(None)), lambda: setattr(report, "generations", report.generations + 1), parallel=True)
    return report
