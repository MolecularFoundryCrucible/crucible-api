from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import join
from sqlalchemy.orm import selectinload
from sqlmodel import and_, or_, Session, select

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, DatasetACL, DatasetSampleLink,
    Sample, SampleLink, User,
)
from app.models.misc import IdType
from app.models.response_schemas import SampleWithDatasetsResponse
from app.utils.auth_utils import authorize_user
from app.utils.sql_utils import add_or_update, generate_kw_query

router = APIRouter(tags=["samples"])
engine = get_engine()


@router.post("/samples/{parent_sample_id}/children/{child_sample_id}", response_model=SampleLink)
def link_parent_child_samples(parent_sample_id, child_sample_id, auth_res: Annotated[tuple, Depends(authorize_user)]):

    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).first()
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).first()
        link = add_or_update(session,
                     SampleLink,
                     where = (and_(SampleLink.parent_sample_id == parent_sample.id, SampleLink.sample_id == child_sample.id)),
                     parent_sample_id = parent_sample.id,
                     sample_id = child_sample.id)

        session.commit()
        session.refresh(link)

        return(link)


@router.patch('/samples/{sample_id}', response_model=Sample)
def update_sample(auth_res: Annotated[tuple, Depends(authorize_user)], sample_id: str, updated_sample: Sample):
    access_group_name, ag_ids = auth_res
    cmd = select(Sample).where(Sample.unique_id == sample_id)
    update_fields = updated_sample.model_dump(exclude_unset=True)
    with Session(engine) as session:
        sample = session.exec(cmd).one_or_none()
        if not sample:
            raise HTTPException(status_code=404, detail='Sample not found')

        for key, value in update_fields.items():
            setattr(sample, key, value)
            print(key, value)
        session.add(sample)
        session.commit()
        session.refresh(sample)
        return sample


@router.post("/samples", response_model=Sample)
def create_sample(auth_res: Annotated[tuple, Depends(authorize_user)], S: Sample):
    """
    Create a new sample in the database.

    Args:
        auth_res: Authorization result containing access group info
        S: Sample object to be added

    Returns:
        Added sample object

    Raises:
        HTTPException: If required sample fields are missing
    """
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if not S.sample_name or not S.sample_name.strip():
        missing_fields.append("sample_name")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required sample information: {', '.join(missing_fields)}. Please provide all required fields: sample_name."
        )

    with Session(engine) as session:
        print("starting sample addition")
        where = or_(Sample.unique_id == S.unique_id,
                and_(Sample.project_id == S.project_id,
                     Sample.sample_name == S.sample_name))
        statement = select(Sample).where(where)
        sample = session.exec(statement).one_or_none()

        print(f'{sample=}')
        if sample:
            return sample

        sample = Sample(**S.model_dump(exclude_unset=True, exclude={"parents", "children"}))
        print(f'{sample=}')
        session.add(sample)
        session.commit()
        session.refresh(sample)

        if sample.owner_user_id is None and sample.owner_orcid is None:
            creator = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).one_or_none()
            if is_orcid(access_group_name):
                sample.owner_orcid = access_group_name
                creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
                sample.owner_user_id = creator_user_id

        elif sample.owner_user_id is None:
            creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
            sample.owner_user_id = creator_user_id

        else:
            pass

        session.add(sample)
        print('readded sample')
        add_or_update(session,
                      IdType,
                      where=(IdType.unique_id == sample.unique_id),
                      unique_id=sample.unique_id,
                      object_type="sample")

        session.commit()
        print('final commit')
        session.refresh(sample)
        return(sample)


@router.get("/samples", response_model=List[SampleWithDatasetsResponse])
def list_samples(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    '''
    Returns a list of samples where the owner_orcid matches the orcid of the person who made the request.
    If an admin API key is used, returns all samples.
    '''
    access_group_name, ag_ids = auth_res

    query_params = request.query_params
    print(query_params)

    params_dict = dict(query_params)
    print(params_dict)

    with Session(engine) as session:

        samp_cmd = select(Sample)

        # Admin API keys can see all samples
        if access_group_name.startswith('admin'):
            print('using admin account')
            pass
        elif is_orcid(access_group_name):
            print(f'filtering on owner_orcid={access_group_name}')
            associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
            associated_group_names = [ag.group_name for ag in associated_groups]
            samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == access_group_name,
                                          Sample.project_id.in_(associated_group_names)))
        else:
            print(f'apikey does not have access to samples')
            return []

        for k, v in params_dict.items():
            if hasattr(Sample, k) and v is not None:
                print(f'filtering results on {k}={v}')
                samp_cmd = samp_cmd.where(getattr(Sample, k) == v)
            else:
               pass

        samp_cmd = samp_cmd.options(selectinload(Sample.datasets))
        samp_list = session.exec(samp_cmd).all()
        return samp_list


@router.get("/samples/{sample_id}", response_model=Union[None, SampleWithDatasetsResponse])
def read_sample(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the sample for the provided ID
    '''
    with Session(engine) as session:
        query = select(Sample).options(selectinload(Sample.datasets)).where(Sample.unique_id == sample_id)
        samp = session.exec(query).first()
        return(samp)


@router.get("/samples/{sample_id}/datasets")
def list_sample_datasets(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, include_metadata: bool = False, limit: Optional[int] = None):
    '''
    Returns a list of all datasets linked to a given sample
    '''
    access_group_name, ag_ids = auth_res

    params_dict = dict(request.query_params)
    keyword_filter = params_dict.pop("keyword", None)

    with Session(engine) as session:
        # Verify sample exists
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()

        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        print(f'{include_metadata=}')
        if include_metadata:
            query = (
                    select(Dataset)
                    .options(selectinload(Dataset.scientific_metadata))
                    .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                    .filter(auth_filter)
                )

        else:
            query = (select(Dataset)
                        .select_from(
                            join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                        )
                        .filter(auth_filter)
                    )

        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)

        for key, value in params_dict.items():
            if hasattr(Dataset, key):
                query = query.where(getattr(Dataset, key) == value)

        full_query = query.join(DatasetSampleLink,
                                DatasetSampleLink.dataset_id == Dataset.id
                                ).where(
                                    DatasetSampleLink.sample_id == select(Sample.id).where(Sample.unique_id == sample_id)
                                ).distinct(Dataset.id)

        if limit is not None:
            full_query = full_query.limit(limit)

        datasets = session.exec(full_query).all()
        if include_metadata:
            return [{**dataset.model_dump(),
                    "scientific_metadata": dataset.scientific_metadata.model_dump() if dataset.scientific_metadata else None
                    }
                    for dataset in datasets]
        else:
            return datasets


@router.get("/samples/{child_sample_id}/parents", response_model=List[Sample])
def list_parent_samples(child_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of parent samples for a given child sample
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()

        if child_sample is None:
            raise HTTPException(status_code=404, detail="Child sample not found")

        parent_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.parent_sample_id == Sample.id))
            .where(SampleLink.sample_id == child_sample.id)
        ).all()
        return parent_samples


@router.get("/samples/{parent_sample_id}/children", response_model=List[Sample])
def list_child_samples(parent_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of child samples for a given parent sample
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()

        if parent_sample is None:
            raise HTTPException(status_code=404, detail="Parent sample not found")

        child_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.sample_id == Sample.id))
            .where(SampleLink.parent_sample_id == parent_sample.id)
        ).all()
        return child_samples
