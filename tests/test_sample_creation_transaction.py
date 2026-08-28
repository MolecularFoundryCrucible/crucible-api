import pytest
from sqlmodel import Session, select

import app.api.routes.samples as samples_routes
from app.models.core_models import Resource, ResourceACL, Sample


def test_sample_creation_rolls_back_every_row_when_acl_creation_fails(
    client,
    db_engine,
    auth_as,
    monkeypatch,
):
    auth_as("0000-0001-2345-6789", groups=[])

    def fail_acl_creation(*args, **kwargs):
        raise RuntimeError("ACL creation failed")

    monkeypatch.setattr(samples_routes, "create_standard_acls", fail_acl_creation)

    with pytest.raises(RuntimeError, match="ACL creation failed"):
        client.post("/samples", json={"sample_name": "atomic-sample"})

    with Session(db_engine) as session:
        assert session.exec(
            select(Sample).where(Sample.sample_name == "atomic-sample")
        ).one_or_none() is None
        assert session.exec(select(Resource)).all() == []
        assert session.exec(select(ResourceACL)).all() == []
