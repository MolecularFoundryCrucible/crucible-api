from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import join
from sqlalchemy.orm import attributes, selectinload
from sqlmodel import Session, SQLModel, select

from app.api.dependencies import get_engine
from app.general_utils import get_tz_isoformat
from app.models.core_models import Dataset, Resource, ResourceACL, ScientificMetadataBinary
from app.utils.auth_utils import authorize_user
from app.utils.sql_utils import add_or_update
from app.models.datasets import ScientificMetadataBinaryRead


router = APIRouter(tags=["metadata"])
engine = get_engine()



@router.get("/metadata/{unique_id}", response_model=ScientificMetadataBinaryRead)
def read_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], unique_id: str):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        query = select(Resource)\
                    .options(selectinload(Resource.scientific_metadata))\
                    .select_from(join(Resource, ResourceACL, ResourceACL.mfid == Resource.mfid, isouter=True))\
                    .outerjoin(Dataset, Dataset.unique_id == Resource.mfid)\
                    .where(Resource.mfid == unique_id)\
                    .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()

        resource = session.exec(query).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found or access denied")
        
        return resource.scientific_metadata


@router.post("/metadata/{unique_id}", response_model=ScientificMetadataBinaryRead)
def add_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], unique_id: str, scientific_md: dict):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    query = select(Resource).select_from(join(Resource,
                                              ResourceACL,
                                              ResourceACL.mfid == Resource.mfid)).where(
                                              Resource.mfid == unique_id).filter(
                                              ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        resource = session.exec(query).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")


        new_md_obj = add_or_update(
            session,
            ScientificMetadataBinary,
            where=(ScientificMetadataBinary.unique_id == unique_id),
            unique_id=unique_id,
            scientific_metadata=scientific_md,
        )

        attributes.flag_modified(new_md_obj, 'scientific_metadata')

        resource.modification_time = get_tz_isoformat()

        session.commit()
        session.refresh(new_md_obj)
        return(new_md_obj)



@router.patch("/metadata/{unique_id}", response_model=ScientificMetadataBinaryRead)
def update_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], unique_id: str, scientific_md_updates: dict):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    query = select(Resource).select_from(join(Resource,
                                              ResourceACL,
                                              ResourceACL.mfid == Resource.mfid)).where(
                                              Resource.mfid == unique_id).filter(
                                              ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        resource = session.exec(query).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        stored_md = session.exec(select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == unique_id)).one_or_none()
        print(f'{stored_md=}')
        if stored_md:
            md_dict = stored_md.scientific_metadata

        else:
            md_dict = {}

        print(f'{md_dict=}')

        # this doesn't allow for updating nested fields without providing the whole tree 
        # from the first level down.
        # could make recursive?
        for k,v in scientific_md_updates.items():
            md_dict[k] = v
        print(f'after update: {md_dict=}')

        new_md_obj = add_or_update(
            session,
            ScientificMetadataBinary,
            where=(ScientificMetadataBinary.unique_id == unique_id),
            unique_id=unique_id,
            scientific_metadata=md_dict,
        )

        attributes.flag_modified(new_md_obj, 'scientific_metadata')
 
        resource.modification_time = get_tz_isoformat()

        session.commit()
        session.refresh(new_md_obj)
        return(new_md_obj)




def _upsert_resource_metadata(session, resource_id: str, scientific_md: dict) -> ScientificMetadataBinary:
    """Upsert scientific metadata for a resource. Caller must commit."""
    new_md_obj = add_or_update(
        session,
        ScientificMetadataBinary,
        where=(ScientificMetadataBinary.unique_id == resource_id),
        unique_id=resource_id,
        scientific_metadata=scientific_md,
    )
    attributes.flag_modified(new_md_obj, 'scientific_metadata')
    ri = session.exec(select(Resource).where(Resource.mfid == resource_id)).one_or_none()
    if ri:
        ri.modification_time = get_tz_isoformat()
    return new_md_obj


def _merge_resource_metadata(session, resource_id: str, updates: dict) -> ScientificMetadataBinary:
    """Merge updates into existing SMB metadata for a resource. Caller must commit."""
    stored = session.exec(
        select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == resource_id)
    ).one_or_none()
    md_dict = {**(stored.scientific_metadata if stored else {}), **updates}
    new_md_obj = add_or_update(
        session,
        ScientificMetadataBinary,
        where=(ScientificMetadataBinary.unique_id == resource_id),
        unique_id=resource_id,
        scientific_metadata=md_dict,
    )
    attributes.flag_modified(new_md_obj, 'scientific_metadata')
    ri = session.exec(select(Resource).where(Resource.mfid == resource_id)).one_or_none()
    if ri:
        ri.modification_time = get_tz_isoformat()
    return new_md_obj


