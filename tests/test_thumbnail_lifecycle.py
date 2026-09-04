import base64
import io
import logging

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlmodel import Session, select

import app.api.routes.datasets as datasets_routes
import app.services.thumbnails as thumbnail_service
from app.models.core_models import Dataset, Resource, Thumbnail
from app.models.datasets import (
    ThumbnailCreate,
    ThumbnailPreviewRead,
    ThumbnailRead,
    ThumbnailUpdate,
)
from app.utils.permissions import PlatformRole, Principal


def _thumbnail_b64() -> str:
    image = Image.new("RGB", (2, 2), color="blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _setup_datasets(db_engine):
    with Session(db_engine) as session:
        session.add_all([
            Dataset(unique_id="dataset-one", dataset_name="Dataset one"),
            Dataset(unique_id="dataset-two", dataset_name="Dataset two"),
            Resource(mfid="dataset-one", resource_type="dataset"),
            Resource(mfid="dataset-two", resource_type="dataset"),
        ])
        session.commit()


def test_identical_thumbnail_uploads_remain_owned_by_their_datasets(
    db_engine,
):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    thumbnail_b64 = _thumbnail_b64()

    first = datasets_routes.add_thumbnail(
        principal,
        "dataset-one",
        ThumbnailCreate(thumbnail_name="first.png", thumbnail_b64str=thumbnail_b64),
    )
    second = datasets_routes.add_thumbnail(
        principal,
        "dataset-two",
        ThumbnailCreate(thumbnail_name="second.png", thumbnail_b64str=thumbnail_b64),
    )

    assert first.id != second.id

    with Session(db_engine) as session:
        datasets = {
            dataset.unique_id: dataset.id
            for dataset in session.exec(select(Dataset)).all()
        }
        thumbnails = session.exec(select(Thumbnail).order_by(Thumbnail.id)).all()

    assert [(thumbnail.thumbnail_name, thumbnail.dataset_id) for thumbnail in thumbnails] == [
        ("first.png", datasets["dataset-one"]),
        ("second.png", datasets["dataset-two"]),
    ]


def test_identical_thumbnail_upload_is_idempotent_within_one_dataset(
    db_engine,
):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    thumbnail_b64 = _thumbnail_b64()

    first = datasets_routes.add_thumbnail(
        principal,
        "dataset-one",
        ThumbnailCreate(thumbnail_name="first.png", thumbnail_b64str=thumbnail_b64),
    )
    repeated = datasets_routes.add_thumbnail(
        principal,
        "dataset-one",
        ThumbnailCreate(thumbnail_name="renamed.png", thumbnail_b64str=thumbnail_b64),
    )

    assert repeated.id == first.id
    assert repeated.thumbnail_name == "renamed.png"

    with Session(db_engine) as session:
        thumbnails = session.exec(select(Thumbnail)).all()

    assert len(thumbnails) == 1


def test_unauthorized_upload_is_rejected_before_image_processing(db_engine):
    _setup_datasets(db_engine)
    principal = Principal(user_id="unauthorized")

    with pytest.raises(HTTPException) as exc_info:
        datasets_routes.add_thumbnail(
            principal,
            "dataset-one",
            ThumbnailCreate(
                thumbnail_name="invalid.png",
                thumbnail_b64str="not an image",
            ),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Dataset not found"


@pytest.mark.parametrize(
    "thumbnail_b64str",
    [
        "not valid base64",
        base64.b64encode(b"not an image").decode("ascii"),
    ],
)
def test_invalid_thumbnail_images_return_a_stable_error(
    db_engine,
    thumbnail_b64str,
):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)

    with pytest.raises(HTTPException) as exc_info:
        datasets_routes.add_thumbnail(
            principal,
            "dataset-one",
            ThumbnailCreate(
                thumbnail_name="invalid.png",
                thumbnail_b64str=thumbnail_b64str,
            ),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid thumbnail image"


def test_thumbnail_source_byte_limit_returns_413(db_engine, monkeypatch):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    monkeypatch.setattr(thumbnail_service, "MAX_SOURCE_BYTES", 1)
    monkeypatch.setattr(thumbnail_service, "MAX_ENCODED_CHARACTERS", 1000)

    with pytest.raises(HTTPException) as exc_info:
        datasets_routes.add_thumbnail(
            principal,
            "dataset-one",
            ThumbnailCreate(
                thumbnail_name="large.png",
                thumbnail_b64str=_thumbnail_b64(),
            ),
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "Thumbnail image is too large"


def test_thumbnail_source_pixel_limit_returns_413(db_engine, monkeypatch):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    monkeypatch.setattr(thumbnail_service, "MAX_SOURCE_PIXELS", 3)

    with pytest.raises(HTTPException) as exc_info:
        datasets_routes.add_thumbnail(
            principal,
            "dataset-one",
            ThumbnailCreate(
                thumbnail_name="large.png",
                thumbnail_b64str=_thumbnail_b64(),
            ),
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "Thumbnail image is too large"


def test_thumbnail_normalization_applies_exif_orientation(db_engine):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    image = Image.new("RGB", (4, 2), color="blue")
    exif = image.getexif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    thumbnail = datasets_routes.add_thumbnail(
        principal,
        "dataset-one",
        ThumbnailCreate(
            thumbnail_name="oriented.jpg",
            thumbnail_b64str=base64.b64encode(buffer.getvalue()).decode("ascii"),
        ),
    )

    with Image.open(io.BytesIO(base64.b64decode(thumbnail.thumbnail_b64str))) as normalized:
        assert normalized.size == (2, 4)


def test_thumbnail_response_models_identify_normalized_jpeg():
    thumbnail = ThumbnailRead(
        id=1,
        dataset_id=2,
        thumbnail_name="preview.png",
        thumbnail_b64str="encoded",
    )
    preview = ThumbnailPreviewRead(
        id=1,
        thumbnail_name="preview.png",
        thumbnail_b64str="encoded",
    )

    assert thumbnail.mime_type == "image/jpeg"
    assert preview.mime_type == "image/jpeg"


def test_thumbnail_list_and_bulk_preview_use_lowest_id_first(db_engine):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    with Session(db_engine) as session:
        dataset = session.exec(
            select(Dataset).where(Dataset.unique_id == "dataset-one")
        ).one()
        session.add_all([
            Thumbnail(
                thumbnail_name="first.jpg",
                thumbnail_b64str="first",
                dataset_id=dataset.id,
            ),
            Thumbnail(
                thumbnail_name="second.jpg",
                thumbnail_b64str="second",
                dataset_id=dataset.id,
            ),
        ])
        session.commit()

    thumbnails = datasets_routes.read_thumbnails(principal, "dataset-one")
    previews = datasets_routes.get_first_thumbnails(principal, ["dataset-one"])

    assert [thumbnail.thumbnail_name for thumbnail in thumbnails] == [
        "first.jpg",
        "second.jpg",
    ]
    assert previews["dataset-one"]["thumbnail_name"] == "first.jpg"


def test_large_bulk_thumbnail_lookup_is_logged_without_rejection(
    db_engine,
    caplog,
    monkeypatch,
):
    _setup_datasets(db_engine)
    principal = Principal(user_id="tracking-user")
    monkeypatch.setattr(datasets_routes, "LARGE_THUMBNAIL_LOOKUP_WARNING_SIZE", 2)

    with caplog.at_level(logging.WARNING, logger=datasets_routes.__name__):
        response = datasets_routes.get_first_thumbnails(
            principal,
            ["dataset-one", "dataset-one", "dataset-two"],
        )

    assert response == {}
    assert "user_id=tracking-user requested=3 unique=2 threshold=2" in caplog.text


def test_thumbnail_delete_conceals_inaccessible_dataset(db_engine):
    _setup_datasets(db_engine)
    with Session(db_engine) as session:
        dataset = session.exec(
            select(Dataset).where(Dataset.unique_id == "dataset-one")
        ).one()
        thumbnail = Thumbnail(
            thumbnail_name="private.jpg",
            thumbnail_b64str="encoded",
            dataset_id=dataset.id,
        )
        session.add(thumbnail)
        session.commit()
        session.refresh(thumbnail)
        thumbnail_id = thumbnail.id

    with pytest.raises(HTTPException) as exc_info:
        datasets_routes.delete_thumbnail(
            Principal(user_id="unauthorized"),
            "dataset-one",
            thumbnail_id,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Dataset not found"
    with Session(db_engine) as session:
        assert session.get(Thumbnail, thumbnail_id) is not None


def test_thumbnail_delete_removes_thumbnail_for_authorized_caller(db_engine):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    with Session(db_engine) as session:
        dataset = session.exec(
            select(Dataset).where(Dataset.unique_id == "dataset-one")
        ).one()
        thumbnail = Thumbnail(
            thumbnail_name="deletable.jpg",
            thumbnail_b64str="encoded",
            dataset_id=dataset.id,
        )
        session.add(thumbnail)
        session.commit()
        session.refresh(thumbnail)
        thumbnail_id = thumbnail.id

    response = datasets_routes.delete_thumbnail(
        principal,
        "dataset-one",
        thumbnail_id,
    )

    assert response == {
        "message": f"Thumbnail {thumbnail_id} deleted from dataset dataset-one"
    }
    with Session(db_engine) as session:
        assert session.get(Thumbnail, thumbnail_id) is None


def test_thumbnail_dataset_id_is_indexed():
    assert "ix_thumbnail_dataset_id" in {
        index.name for index in Thumbnail.__table__.indexes
    }


def test_thumbnail_update_can_replace_name_and_image(db_engine):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    original = datasets_routes.add_thumbnail(
        principal,
        "dataset-one",
        ThumbnailCreate(
            thumbnail_name="original.png",
            thumbnail_b64str=_thumbnail_b64(),
        ),
    )
    replacement = Image.new("RGB", (3, 1), color="red")
    buffer = io.BytesIO()
    replacement.save(buffer, format="PNG")

    updated = datasets_routes.update_thumbnail(
        principal,
        "dataset-one",
        original.id,
        ThumbnailUpdate(
            thumbnail_name="replacement.png",
            thumbnail_b64str=base64.b64encode(buffer.getvalue()).decode("ascii"),
        ),
    )

    assert updated.id == original.id
    assert updated.thumbnail_name == "replacement.png"
    with Image.open(io.BytesIO(base64.b64decode(updated.thumbnail_b64str))) as image:
        assert image.size == (3, 1)


def test_thumbnail_update_verifies_dataset_ownership(db_engine):
    _setup_datasets(db_engine)
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    thumbnail = datasets_routes.add_thumbnail(
        principal,
        "dataset-two",
        ThumbnailCreate(
            thumbnail_name="other.png",
            thumbnail_b64str=_thumbnail_b64(),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        datasets_routes.update_thumbnail(
            principal,
            "dataset-one",
            thumbnail.id,
            ThumbnailUpdate(thumbnail_name="wrong-dataset.png"),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Thumbnail not found"
