from enum import Enum
import os
from typing import Annotated, List, Optional, Union
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import func, join, literal, text
from sqlmodel import Session, select

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.general_utils import get_utc_isoformat
from app.models.access_groups import AccessGroupMemberRead
from app.models.core_models import AccessGroup, Instrument, Resource, ScientificMetadataBinary, User, UserAccessGroupLink
from app.models.instrument import InstrumentCreate, InstrumentRead, InstrumentResponse, InstrumentUpdate
from app.models.pagination import PaginatedResponse
from app.models.users import UserPublicRead
from app.utils.auth_utils import authorize_user, require_permission, resource_capabilities
from app.utils.instrument_utils import assert_instrument_id_available
from app.utils.permissions import AccessGroupType, Permission, Principal, Role
from app.utils.sql_utils import add_or_update, create_standard_acls, sql_create_resource_acl, touch_resource_modification_time
from app.utils.user_utils import resolve_create_owner
from app.utils.gcs_record_utils import write_record, instrument_record

router = APIRouter(tags=["instruments"])
engine = get_engine()

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)

INSTRUMENT_FILTER_FIELDS = {
    "other_id",
    "other_id_source",
    "instrument_name",
    "manufacturer",
    "model",
    "owner",
    "location",
    "description",
    "instrument_type",
    "status",
}


class InstrumentStatus(str, Enum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


VALID_INSTRUMENT_STATUSES = tuple(status.value for status in InstrumentStatus)


def _instrument_capabilities(session, principal, instrument):
    legacy_group = (instrument.instrument_name or "").lower()
    return resource_capabilities(
        session,
        principal,
        instrument.unique_id,
        legacy_edit_group=legacy_group,
        legacy_status_group=legacy_group,
    )


def _instrument_response(instrument, resource=None, scientific_metadata=None, owner=None,
                         capabilities=None):
    return InstrumentResponse.model_validate({
        **instrument.model_dump(exclude={"owner"}),
        "owner_orcid": instrument.owner,
        "owner": owner,
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": scientific_metadata,
        "capabilities": capabilities,
    })


def _instrument_owners(session, instruments):
    owner_ids = {instrument.owner for instrument in instruments if instrument.owner}
    if not owner_ids:
        return {}
    return {
        user.unique_id: UserPublicRead.model_validate(user)
        for user in session.exec(select(User).where(User.unique_id.in_(owner_ids))).all()
    }

@router.get("/instruments", response_model=PaginatedResponse[InstrumentResponse])
def list_instruments(auth_res: Annotated[Principal, Depends(authorize_user)], request: Request,
                     instrument_id: Optional[str] = Query(default=None, description="Exact instrument slug."),
                     include_metadata: bool = False,
                     include_owner: bool = False,
                     limit: int = Query(default=100, ge=1, le=1000),
                     offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of instruments filtered by any provided query parameters that
    match Instrument attributes. Matching is case-insensitive exact match.
    Use instrument_id for exact slug lookup. Other filterable fields include
    other_id, other_id_source, instrument_name, manufacturer,
    model, owner, location, description, instrument_type, status.
    Defaults to status=active; pass ?status=maintenance or ?status=decommissioned to see others.
    An exact instrument_id lookup searches every status unless status is also provided.
    '''
    query_params = dict(request.query_params)
    for k in ('instrument_id', 'include_metadata', 'include_owner', 'limit', 'offset'):
        query_params.pop(k, None)
    if instrument_id is None:
        query_params.setdefault('status', 'active')
    filters = [
        func.lower(getattr(Instrument, key)) == value.lower().strip()
        for key, value in query_params.items()
        if key in INSTRUMENT_FILTER_FIELDS
    ]
    if instrument_id is not None:
        filters.append(func.lower(Instrument.instrument_id) == instrument_id.lower().strip())

    with Session(engine) as session:
        base = select(Instrument).join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
        for f in filters:
            base = base.where(f)

        total = session.exec(select(func.count()).select_from(base.subquery())).one()

        if include_metadata:
            statement = (
                select(Instrument, Resource, ScientificMetadataBinary)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Instrument.unique_id, isouter=True)
            )
        else:
            statement = select(Instrument, Resource).join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)

        for f in filters:
            statement = statement.where(f)

        rows = session.exec(statement.order_by(Resource.creation_time.desc()).offset(offset).limit(limit)).all()
        owners = _instrument_owners(session, (row[0] for row in rows)) if include_owner else {}

        items = []
        for row in rows:
            if include_metadata:
                inst, ri, smb = row
                items.append(_instrument_response(
                    inst,
                    ri,
                    smb.scientific_metadata if smb else None,
                    owners.get(inst.owner),
                    _instrument_capabilities(session, auth_res, inst) if instrument_id is not None else None,
                ))
            else:
                inst, ri = row
                items.append(_instrument_response(
                    inst,
                    ri,
                    owner=owners.get(inst.owner),
                    capabilities=_instrument_capabilities(session, auth_res, inst) if instrument_id is not None else None,
                ))
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/instruments/search", response_model=PaginatedResponse[InstrumentResponse])
def search_instruments(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Search term matched against instrument_name, instrument_type, and manufacturer."),
    status: Optional[InstrumentStatus] = Query(default=None, description="Restrict results to one lifecycle status."),
    include_owner: bool = False,
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search instruments by name, type, or manufacturer using fuzzy matching.
    Returns results ordered by match quality. Maximum 50 results.
    '''
    with Session(engine) as session:
        session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {get_trgm_search_threshold()}"))
        base = (
            select(Instrument, Resource)
            .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
        )
        if status is not None:
            base = base.where(Instrument.status == status.value)
        for token in q.lower().split():
            base = base.where(
                Instrument.instrument_name.ilike(f"{token}%") |
                literal(token).op("<%")(Instrument.instrument_name) |
                literal(token).op("<%")(Instrument.instrument_type) |
                literal(token).op("<%")(Instrument.manufacturer)
            )
        statement = base.order_by(
            func.greatest(
                func.word_similarity(q, Instrument.instrument_name),
                func.coalesce(func.word_similarity(q, Instrument.instrument_type), 0),
                func.coalesce(func.word_similarity(q, Instrument.manufacturer), 0),
            ).desc()
        ).limit(limit)
        rows = session.exec(statement).all()
        owners = _instrument_owners(session, (inst for inst, _ in rows)) if include_owner else {}
        items = [
            _instrument_response(inst, ri, owner=owners.get(inst.owner))
            for inst, ri in rows
        ]
        return PaginatedResponse(limit=limit, items=items)


@router.get("/instruments/{instrument_mfid}", response_model=InstrumentResponse)
def read_instrument(auth_res: Annotated[Principal, Depends(authorize_user)], instrument_mfid: str,
                    include_metadata: bool = False, include_owner: bool = False):
    '''
    Returns the instrument with the provided unique ID, or 404 if not found.
    '''
    with Session(engine) as session:
        if include_metadata:
            result = session.exec(
                select(Instrument, Resource, ScientificMetadataBinary)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Instrument.unique_id, isouter=True)
                .where(Instrument.unique_id == instrument_mfid)
            ).one_or_none()
            if result is None:
                raise HTTPException(status_code=404, detail="Instrument not found")
            inst, ri, smb = result
            owner = _instrument_owners(session, [inst]).get(inst.owner) if include_owner else None
            return _instrument_response(
                inst,
                ri,
                smb.scientific_metadata if smb else None,
                owner,
                _instrument_capabilities(session, auth_res, inst),
            )
        else:
            result = session.exec(
                select(Instrument, Resource)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .where(Instrument.unique_id == instrument_mfid)
            ).one_or_none()
            if result is None:
                raise HTTPException(status_code=404, detail="Instrument not found")
            inst, ri = result
            owner = _instrument_owners(session, [inst]).get(inst.owner) if include_owner else None
            return _instrument_response(
                inst,
                ri,
                owner=owner,
                capabilities=_instrument_capabilities(session, auth_res, inst),
            )


@router.post("/instruments", response_model=InstrumentResponse)
def create_instrument(auth_res: Annotated[Principal, Depends(authorize_user)], instrument: InstrumentCreate,
                      background_tasks: BackgroundTasks):
    '''
    Register a new instrument in the database.
    '''
    if auth_res.is_service_account:
        raise HTTPException(
            status_code=403,
            detail="Service accounts cannot register instruments",
        )

    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        assert_instrument_id_available(session, instrument.instrument_id)

        instrument_details = instrument.model_dump(exclude_unset=True)
        owner_user = resolve_create_owner(
            session,
            instrument_details.pop("owner", None),
            instrument_details.pop("owner_orcid", None),
            user_unique_id,
        )
        if (
            owner_user.unique_id != user_unique_id
            and not auth_res.is_platform_admin
        ):
            raise HTTPException(
                status_code=403,
                detail="Only platform administrators may create an instrument on behalf of another user",
            )

        instrument_details["owner"] = owner_user.unique_id
        db_instrument = Instrument(**instrument_details)
        session.add(db_instrument)
        session.flush()
        now = get_utc_isoformat()
        existing_ri = session.exec(select(Resource).where(Resource.mfid == db_instrument.unique_id)).one_or_none()
        ri = add_or_update(session, Resource,
                           where=(Resource.mfid == db_instrument.unique_id),
                           mfid=db_instrument.unique_id,
                           resource_type="instrument",
                           created_with_api_version = API_VERSION,
                           api_version_githash = API_VERSION_GITHASH)
        if existing_ri is None:
            ri.creation_time = now
        ri.modification_time = now

        ag = add_or_update(session, AccessGroup,
                           AccessGroup.group_name == db_instrument.unique_id,
                           group_name=db_instrument.unique_id,
                           group_type=AccessGroupType.INSTRUMENT.value)
        create_standard_acls(
            session,
            db_instrument.unique_id,
            owner_id=owner_user.unique_id,
            requestor_id=user_unique_id,
        )
        sql_create_resource_acl(ag, db_instrument.unique_id, read=True, write=True,
                                session=session, permission=Role.EDITOR)

        session.commit()
        session.refresh(db_instrument)
        background_tasks.add_task(write_record, db_instrument.unique_id, instrument_record(db_instrument, ri), user_unique_id, "create")
        return _instrument_response(
            db_instrument,
            ri,
            owner=UserPublicRead.model_validate(owner_user),
            capabilities=_instrument_capabilities(session, auth_res, db_instrument),
        )


@router.patch("/instruments/{instrument_mfid}", response_model=InstrumentResponse)
def update_instrument(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    instrument_mfid: str,
    instrument_update: InstrumentUpdate,
    background_tasks: BackgroundTasks,
):
    '''
    Partially update an instrument record. Only provided fields are modified.
    '''
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        instrument = session.exec(
            select(Instrument).where(Instrument.unique_id == instrument_mfid)
        ).one_or_none()

        if instrument is None:
            raise HTTPException(status_code=404, detail="Instrument not found")

        # Editor on the instrument record. legacy_group covers instruments that
        # have no ACL rows yet: their group is named after the instrument name.
        require_permission(session, auth_res, instrument_mfid, Permission.EDIT,
                           legacy_group=(instrument.instrument_name or "").lower())

        update_data = instrument_update.model_dump(exclude_unset=True)
        if update_data.get("instrument_id"):
            assert_instrument_id_available(session, update_data["instrument_id"],
                                           exclude_unique_id=instrument_mfid)

        for key, value in update_data.items():
            setattr(instrument, key, value)

        ri = session.exec(select(Resource).where(Resource.mfid == instrument_mfid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_isoformat()

        session.add(instrument)
        session.commit()
        session.refresh(instrument)
        background_tasks.add_task(write_record, instrument.unique_id, instrument_record(instrument, ri), user_unique_id, "update")
        return _instrument_response(
            instrument,
            ri,
            capabilities=_instrument_capabilities(session, auth_res, instrument),
        )


@router.post("/instruments/{instrument_mfid}/status", response_model=InstrumentResponse)
def set_instrument_status(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    instrument_mfid: str,
    status: str,
):
    """Change an instrument's lifecycle status.

    Its own route rather than a field on PATCH: decommissioning is a lifecycle
    transition, not a metadata edit, and carries a higher bar. Instruments are
    never deleted, so this is how one is retired.
    """
    if status not in VALID_INSTRUMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {list(VALID_INSTRUMENT_STATUSES)}")

    with Session(engine) as session:
        instrument = session.exec(
            select(Instrument).where(Instrument.unique_id == instrument_mfid)
        ).one_or_none()
        if instrument is None:
            raise HTTPException(status_code=404, detail="Instrument not found")

        # admin, not editor: retiring a machine is not a metadata edit, and the
        # instrument's own service account holds only editor.
        require_permission(session, auth_res, instrument_mfid, Permission.DELETE,
                           legacy_group=(instrument.instrument_name or "").lower())

        instrument.status = status
        session.add(instrument)
        touch_resource_modification_time(session, instrument_mfid)
        session.commit()
        session.refresh(instrument)

        ri = session.exec(select(Resource).where(
            Resource.mfid == instrument_mfid)).one_or_none()
        return _instrument_response(
            instrument,
            ri,
            capabilities=_instrument_capabilities(session, auth_res, instrument),
        )


def _get_instrument_operators(instrument_mfid: str, session) -> List[AccessGroupMemberRead]:
    '''Query the instrument's operator group members. No auth check, for internal use only.'''
    rows = session.exec(
        select(User, UserAccessGroupLink.role)
        .select_from(join(AccessGroup, UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id))
        .join(User, User.id == UserAccessGroupLink.user_id)
        .where(
            AccessGroup.group_name == instrument_mfid,
            AccessGroup.group_type == AccessGroupType.INSTRUMENT.value,
            User.is_service_account == True,
        )
    ).all()
    return [
        AccessGroupMemberRead.model_validate(user, update={"role": role or "unknown"})
        for user, role in rows
    ]


@router.get("/instruments/{instrument_mfid}/service_accounts", response_model=List[AccessGroupMemberRead])
def list_instrument_service_accounts(
    instrument_mfid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    with Session(engine) as session:
        instrument = session.exec(
            select(Instrument).where(Instrument.unique_id == instrument_mfid)
        ).one_or_none()
        if instrument is None:
            raise HTTPException(status_code=404, detail="Instrument not found")

        require_permission(session, auth_res, instrument_mfid, Permission.MANAGE_ACL)
        return _get_instrument_operators(instrument_mfid, session)


@router.post("/instruments/{instrument_mfid}/service_accounts/{sa_unique_id}", response_model=List[AccessGroupMemberRead])
def add_instrument_service_account(
    instrument_mfid: str,
    sa_unique_id: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''
    Bind a service account to an instrument's operator group, at admin standing.
    Requires admin on the instrument.
    '''
    with Session(engine) as session:
        instrument = session.exec(select(Instrument).where(Instrument.unique_id == instrument_mfid)).one_or_none()
        if instrument is None:
            raise HTTPException(status_code=404, detail="Instrument not found")

        require_permission(session, auth_res, instrument_mfid, Permission.MANAGE_ACL)

        sa = session.exec(select(User).where(
            User.unique_id == sa_unique_id, User.is_service_account == True)).one_or_none()
        if sa is None:
            raise HTTPException(status_code=404, detail="Service account not found")

        ag = add_or_update(session, AccessGroup,
                           AccessGroup.group_name == instrument_mfid,
                           group_name=instrument_mfid,
                           group_type=AccessGroupType.INSTRUMENT.value)
        session.flush()
        link = session.exec(
            select(UserAccessGroupLink)
            .where(UserAccessGroupLink.user_id == sa.id, UserAccessGroupLink.accessgroup_id == ag.id)
        ).one_or_none()
        if link is None:
            session.add(UserAccessGroupLink(user_id=sa.id, accessgroup_id=ag.id, role=Role.ADMIN.db_value))
        elif link.role != Role.ADMIN.db_value:
            link.role = Role.ADMIN.db_value
            session.add(link)
        session.commit()

        return _get_instrument_operators(instrument_mfid, session)


@router.delete("/instruments/{instrument_mfid}/service_accounts/{sa_unique_id}", response_model=List[AccessGroupMemberRead])
def remove_instrument_service_account(
    instrument_mfid: str,
    sa_unique_id: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''
    Unbind a service account from an instrument's operator group.
    Requires admin on the instrument.
    '''
    with Session(engine) as session:
        instrument = session.exec(select(Instrument).where(Instrument.unique_id == instrument_mfid)).one_or_none()
        if instrument is None:
            raise HTTPException(status_code=404, detail="Instrument not found")

        require_permission(session, auth_res, instrument_mfid, Permission.MANAGE_ACL)

        sa = session.exec(select(User).where(
            User.unique_id == sa_unique_id,
            User.is_service_account == True,
        )).one_or_none()
        if sa is None:
            raise HTTPException(status_code=404, detail="Service account not found")

        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == instrument_mfid)).one_or_none()
        link = session.exec(
            select(UserAccessGroupLink)
            .where(UserAccessGroupLink.user_id == sa.id, UserAccessGroupLink.accessgroup_id == ag.id)
        ).one_or_none() if ag else None
        if link is None:
            raise HTTPException(
                status_code=404,
                detail=f"'{sa_unique_id}' is not bound to instrument '{instrument_mfid}'")
        session.delete(link)
        session.commit()

        return _get_instrument_operators(instrument_mfid, session)
