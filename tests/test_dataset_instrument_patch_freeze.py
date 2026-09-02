import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlmodel import Session

import app.api.routes.datasets as datasets_routes
from app.api.routes.datasets import update_dataset
from app.models.core_models import Dataset, Instrument, Resource
from app.models.datasets import DatasetUpdate
from app.utils.permissions import PlatformRole, Principal


@pytest.fixture(autouse=True)
def disable_record_writes(monkeypatch):
    monkeypatch.setattr(datasets_routes, "write_record", lambda *args, **kwargs: None)


def _setup(db_engine, *, legacy=False, assigned=True):
    with Session(db_engine) as session:
        instrument = Instrument(
            unique_id="instrument-mfid",
            instrument_id="xrd-1",
            instrument_name="XRD 1",
            owner="facility",
            location="b30",
        )
        session.add(instrument)
        session.commit()
        session.refresh(instrument)

        dataset = Dataset(
            unique_id="dataset-mfid",
            dataset_name="before",
            instrument_id=instrument.id if assigned and not legacy else None,
            instrument_mfid=instrument.unique_id if assigned and not legacy else None,
            instrument_name=instrument.instrument_name if assigned else None,
        )
        session.add(dataset)
        session.add(Resource(mfid=dataset.unique_id, resource_type="dataset", public=False))
        session.commit()


def test_dataset_update_instrument_fields_are_deprecated():
    properties = DatasetUpdate.model_json_schema()["properties"]
    assert properties["instrument_id"]["deprecated"] is True
    assert properties["instrument_name"]["deprecated"] is True


def test_patch_accepts_current_instrument_values(db_engine):
    _setup(db_engine)

    response = update_dataset(
        auth_res=Principal(user_id="admin", platform_role=PlatformRole.ADMIN),
        dsid="dataset-mfid",
        updated_dataset=DatasetUpdate(
            dataset_name="after",
            instrument_id="xrd-1",
            instrument_name="xrd 1",
        ),
        background_tasks=BackgroundTasks(),
    )

    assert response.dataset_name == "after"
    assert response.instrument_id == "xrd-1"


def test_patch_rejects_changed_or_cleared_instrument(db_engine):
    _setup(db_engine)
    principal = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    for payload in (
        {"instrument_id": "xrd-2"},
        {"instrument_name": "XRD 2"},
        {"instrument_id": None},
        {"instrument_name": None},
    ):
        with pytest.raises(HTTPException) as exc:
            update_dataset(
                auth_res=principal,
                dsid="dataset-mfid",
                updated_dataset=DatasetUpdate(**payload),
                background_tasks=BackgroundTasks(),
            )
        assert exc.value.status_code == 409


def test_patch_accepts_current_legacy_instrument_name(db_engine):
    _setup(db_engine, legacy=True)

    response = update_dataset(
        auth_res=Principal(user_id="admin", platform_role=PlatformRole.ADMIN),
        dsid="dataset-mfid",
        updated_dataset=DatasetUpdate(dataset_name="after", instrument_name="xrd 1"),
        background_tasks=BackgroundTasks(),
    )

    assert response.dataset_name == "after"


def test_patch_accepts_null_instrument_values_for_unassigned_dataset(db_engine):
    _setup(db_engine, assigned=False)

    response = update_dataset(
        auth_res=Principal(user_id="admin", platform_role=PlatformRole.ADMIN),
        dsid="dataset-mfid",
        updated_dataset=DatasetUpdate(instrument_id=None, instrument_name=None),
        background_tasks=BackgroundTasks(),
    )

    assert response.instrument_id is None
