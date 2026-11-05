import csv
import importlib.util
from pathlib import Path

from sqlmodel import Session, select

from app.models.core_models import Dataset, Instrument, Resource, Sample, User
from app.utils.permissions import PlatformRole

OLD = "0000-0002-1825-0097"
NEW = "0tmnyhfztdwc1000w2kspzsnh9"


def _seed(db_engine):
    with Session(db_engine) as session:
        user = User(unique_id=OLD, first_name="Ada", last_name="Lovelace", email="Ada@Example.com", username="ada")
        session.add(user)
        session.flush()
        session.add_all([
            Dataset(unique_id="ds-owned", dataset_name="owned", owner_orcid=OLD, owner_user_id=user.id),
            Dataset(unique_id="ds-ghost", dataset_name="ghost", owner_orcid="0000-0009-0000-0009"),
            Sample(unique_id="sa-owned", sample_name="owned", owner_orcid=OLD, owner_user_id=user.id),
            Resource(mfid="ds-owned", resource_type="dataset"),
            Resource(mfid="ds-ghost", resource_type="dataset"),
            Resource(mfid="sa-owned", resource_type="sample"),
        ])
        session.commit()
        user.unique_id = NEW
        session.commit()


def _ids(response):
    return sorted(item["unique_id"] for item in response.json()["items"])


def test_dataset_and_sample_owners_follow_the_person(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as("viewer", platform_role=PlatformRole.ADMIN)
    datasets = {item["unique_id"]: item for item in client.get("/datasets", params={"include_owner": "true"}).json()["items"]}
    assert (datasets["ds-owned"]["owner_orcid"], datasets["ds-owned"]["owner"]["unique_id"]) == (NEW, NEW)
    assert (datasets["ds-ghost"]["owner_orcid"], datasets["ds-ghost"]["owner"]) == (None, None)
    assert client.get("/resources/ds-owned").json()["owner_orcid"] == NEW
    assert client.get("/resources/sa-owned").json()["owner_orcid"] == NEW
    assert [item["owner_orcid"] for item in client.get("/samples").json()["items"]] == [NEW]


def test_owner_filters_take_an_id_username_or_email(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as("viewer", platform_role=PlatformRole.ADMIN)
    for reference in (NEW, "ada", "ADA", "ada@example.com"):
        assert _ids(client.get("/datasets", params={"owner_id": reference})) == ["ds-owned"], reference
        assert _ids(client.get("/samples", params={"owner_id": reference})) == ["sa-owned"], reference
    for nobody in (OLD, "0000-0009-0000-0009", "someone", "nobody@example.com"):
        assert _ids(client.get("/datasets", params={"owner_id": nobody})) == [], nobody
    assert _ids(client.get("/datasets", params={"owner_id_is_null": "true"})) == ["ds-ghost"]
    assert _ids(client.get("/datasets", params={"owner_orcid": "ada"})) == ["ds-owned"]


def test_owner_facet_groups_by_the_current_unique_id(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as("viewer", platform_role=PlatformRole.ADMIN)
    buckets = client.get("/datasets/facets", params={"field": "owner"}).json()["items"]
    assert {(bucket["value"], bucket["count"]) for bucket in buckets} == {(NEW, 1), (None, 1)}
    owned = next(bucket for bucket in buckets if bucket["value"] == NEW)
    assert (owned["label"], owned["owner"]["username"]) == ("Ada Lovelace", "ada")
    assert client.get("/datasets/facets", params={"field": "owner", "owner_id": "ada"}).json()["items"][0]["count"] == 1


def _fill_script():
    path = Path(__file__).resolve().parents[1] / "migrations/data/20261105_fill_owner_user_id.py"
    spec = importlib.util.spec_from_file_location("fill_owner_user_id", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fill_script_sets_matching_owners_and_lists_the_rest(db_engine, use_engine, tmp_path, capsys):
    use_engine(db_engine)
    with Session(db_engine) as session:
        user = User(unique_id=OLD, first_name="Ada", last_name="L", email=None, username="ada")
        session.add(user)
        session.flush()
        session.add_all([
            Dataset(unique_id="ds-unfilled", dataset_name="a", owner_orcid=OLD),
            Dataset(unique_id="ds-ghost", dataset_name="b", owner_orcid="ghost"),
            Dataset(unique_id="ds-done", dataset_name="c", owner_orcid=OLD, owner_user_id=user.id),
            Dataset(unique_id="ds-none", dataset_name="d"),
            Sample(unique_id="sa-unfilled", sample_name="e", owner_orcid=OLD),
            Instrument(unique_id="in-free", instrument_name="f", owner="Foundry", location="x"),
        ])
        session.commit()
        user_id = user.id
    script = _fill_script()
    report = tmp_path / "unmatched.csv"

    assert script.main(["--unmatched", str(report)]) == 0
    assert "dataset: dry run, would fill: 1, owner names no user: 1" in capsys.readouterr().out
    with Session(db_engine) as session:
        assert session.exec(select(Dataset.owner_user_id).where(Dataset.unique_id == "ds-unfilled")).one() is None

    assert script.main(["--unmatched", str(report), "--commit"]) == 0
    with Session(db_engine) as session:
        owners = dict(session.exec(select(Dataset.unique_id, Dataset.owner_user_id)).all())
        assert owners == {"ds-unfilled": user_id, "ds-ghost": None, "ds-done": user_id, "ds-none": None}
        assert session.exec(select(Sample.owner_user_id)).one() == user_id
    with open(report) as handle:
        assert list(csv.reader(handle))[1:] == [["dataset", "ds-ghost", "ghost"], ["instrument", "in-free", "Foundry"]]

    assert script.main(["--unmatched", str(report), "--commit"]) == 0
    assert "dataset: filled: 0, owner names no user: 1" in capsys.readouterr().out.splitlines()[-4]
