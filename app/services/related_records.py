"""Full responses for records listed through a relationship (parents, children, a sample's datasets).
They carry everything a list item does except links, so a relationship never nests further."""
from sqlmodel import Session, select

from app.models.datasets import DatasetResponse
from app.models.samples import SampleResponse
from app.services.resource_access import record_roles
from app.tables import Resource
from app.utils.instrument_utils import dataset_instrument_fields, resolve_instrument_mfids_to_references
from app.utils.permissions import Principal
from app.utils.project_utils import resolve_project_mfids_to_references
from app.utils.sql_utils import public_grant_exists
from app.utils.user_utils import owner_fields, owners_of


def _common(session: Session, principal: Principal, records, include_owner: bool):
    mfids = [record.unique_id for record in records]
    resources = {
        mfid: (created, modified, public)
        for mfid, created, modified, public in session.exec(
            select(Resource.mfid, Resource.creation_time, Resource.modification_time, public_grant_exists(Resource.mfid))
            .where(Resource.mfid.in_(mfids))
        ).all()
    } if mfids else {}
    projects = resolve_project_mfids_to_references((record.project_mfid for record in records), session)
    owners = owners_of(session, records)
    roles = record_roles(session, principal, mfids)

    def fields(record) -> dict:
        created, modified, public = resources.get(record.unique_id, (None, None, False))
        project = projects.get(record.project_mfid)
        return {
            **record.model_dump(),
            **owner_fields(record, owners, include_owner),
            "project_id": project.project_id if project else None,
            "project": project,
            "public": bool(public),
            "creation_time": created,
            "modification_time": modified,
            "role": roles.get(record.unique_id),
        }
    return fields


def dataset_responses(session: Session, principal: Principal, datasets, include_owner: bool = True) -> list[DatasetResponse]:
    datasets = list(datasets)
    fields = _common(session, principal, datasets, include_owner)
    instruments = resolve_instrument_mfids_to_references((d.instrument_mfid for d in datasets), session)
    return [
        DatasetResponse.model_validate({**fields(d), **dataset_instrument_fields(d, instruments.get(d.instrument_mfid))})
        for d in datasets
    ]


def sample_responses(session: Session, principal: Principal, samples, include_owner: bool = True) -> list[SampleResponse]:
    samples = list(samples)
    fields = _common(session, principal, samples, include_owner)
    return [SampleResponse.model_validate(fields(s)) for s in samples]
