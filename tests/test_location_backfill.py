import pytest
from sqlmodel import Session, select

from app.services.location_backfill import backfill_locations
from app.tables import AssociatedFile, Dataset, Resource
from tests.test_multipart_lifecycle import MemoryStorage

DS = "ds"


@pytest.fixture
def store(db_engine, monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET", "dedicated")
    with Session(db_engine) as session:
        session.add_all([Resource(mfid=DS, resource_type="dataset"), Dataset(unique_id=DS, dataset_name="data")])
        session.commit()
    storage = MemoryStorage()
    for key in (f"{DS}/a.txt", f"{DS}/raw/b.txt", f"{DS}/x/dup.txt", f"{DS}/y/dup.txt", f"{DS}/_files/upload"):
        storage.objects[key] = {"generation": "7", "size": 1, "crc32c": None, "data": b"x"}
    return storage


def add(engine, filename, storage_path=None, generation=None, store_id=None):
    with Session(engine) as session:
        dataset_id = session.exec(select(Dataset.id)).one()
        file = AssociatedFile(dataset_id=dataset_id, filename=filename, size=1, sha256_hash="a" * 64, storage_path=storage_path, storage_generation=generation, store_id=store_id)
        session.add(file)
        session.commit()
        return file.mfid


def row(engine, mfid):
    with Session(engine) as session:
        file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one()
        return file.storage_path, file.storage_generation, file.store_id


def test_a_dry_run_reports_and_writes_nothing(db_engine, store):
    mfid = add(db_engine, "a.txt")
    report = backfill_locations(db_engine, store, commit=False)
    assert report.filled == 1
    assert row(db_engine, mfid) == (None, None, None)


def test_commit_fills_path_generation_and_store(db_engine, store):
    exact = add(db_engine, "a.txt")
    by_basename = add(db_engine, "b.txt")
    report = backfill_locations(db_engine, store, commit=True)
    assert (report.filled, report.ambiguous, report.missing) == (2, [], [])
    assert row(db_engine, exact) == ("dedicated/ds/a.txt", "7", "default")
    assert row(db_engine, by_basename) == ("dedicated/ds/raw/b.txt", "7", "default")


def test_a_path_without_generation_gets_its_generation(db_engine, store):
    mfid = add(db_engine, "renamed.txt", storage_path="dedicated/ds/a.txt")
    backfill_locations(db_engine, store, commit=True)
    assert row(db_engine, mfid) == ("dedicated/ds/a.txt", "7", "default")


def test_complete_rows_only_get_their_store(db_engine, store):
    complete = add(db_engine, "c.txt", storage_path="dedicated/ds/_files/upload", generation="7")
    elsewhere = add(db_engine, "d.txt", storage_path="mf-storage-prod/ds/d.txt")
    report = backfill_locations(db_engine, store, commit=True)
    assert report.already == 2
    assert row(db_engine, complete) == ("dedicated/ds/_files/upload", "7", "default")
    assert row(db_engine, elsewhere) == ("mf-storage-prod/ds/d.txt", None, "default")


def test_ambiguous_and_missing_rows_are_reported_and_left(db_engine, store):
    ambiguous = add(db_engine, "dup.txt")
    missing = add(db_engine, "none.txt")
    gone = add(db_engine, "gone.txt", storage_path="dedicated/ds/gone.txt")
    report = backfill_locations(db_engine, store, commit=True)
    assert report.ambiguous == [ambiguous]
    assert sorted(report.missing) == sorted([missing, gone])
    assert row(db_engine, ambiguous) == (None, None, None)
    assert row(db_engine, gone) == ("dedicated/ds/gone.txt", None, None)


def test_catalog_only_records_are_not_touched(db_engine, store):
    with Session(db_engine) as session:
        dataset_id = session.exec(select(Dataset.id)).one()
        session.add(AssociatedFile(dataset_id=dataset_id, filename="h5", size=1, sha256_hash="a" * 64, storage_backend="nersc", storage_path="/global/x"))
        session.commit()
    report = backfill_locations(db_engine, store, commit=True)
    assert (report.filled, report.already, report.ambiguous, report.missing) == (0, 0, [], [])


def test_the_command_reports_and_fails_on_unlocated_files(db_engine, store, monkeypatch, capsys):
    from app.cli.__main__ import main
    add(db_engine, "a.txt")
    add(db_engine, "dup.txt")
    monkeypatch.setattr("app.config.check_settings", lambda: None)
    monkeypatch.setattr("app.storage.get_store", store)
    assert main(["files", "backfill-locations"]) == 1
    output = capsys.readouterr().out
    assert "would fill 1 locations" in output and "ambiguous:" in output


def test_a_replace_finishing_during_the_backfill_is_not_undone(db_engine, store, monkeypatch):
    mfid = add(db_engine, "a.txt")
    looked_up = store.head

    def replaced_meanwhile(bucket, key):
        with Session(db_engine) as session:
            file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one()
            file.storage_path, file.storage_generation, file.store_id = "dedicated/ds/_files/new", "9", "default"
            session.add(file)
            session.commit()
        return looked_up(bucket, key)

    monkeypatch.setattr(store, "head", replaced_meanwhile)
    report = backfill_locations(db_engine, store, commit=True)
    assert report.filled == 0
    assert row(db_engine, mfid) == ("dedicated/ds/_files/new", "9", "default")
