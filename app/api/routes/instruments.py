import os
from typing import Annotated, List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, join, or_
from sqlmodel import Session, select

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.api.path_params import InstrumentRef, ServiceAccountRef
from app.general_utils import get_utc_datetime
from app.models.access_groups import AccessGroupMemberRead
from app.models.core_models import (
    AccessGroup, Instrument, Resource, ResourceACL, ScientificMetadataBinary, User, UserAccessGroupLink)
from app.models.instrument import InstrumentCreate, InstrumentRead, InstrumentResponse, InstrumentUpdate
from app.models.pagination import PaginatedResponse
from app.models.query_params import (
    InstrumentAffiliation, InstrumentDetailQuery, InstrumentListQuery,
    InstrumentSearchQuery, InstrumentStatus)
from app.models.users import UserPublicRead
from app.services.resource_access import load_member_grants, may_view_members, record_role, with_grant_capabilities
from app.services.resource_details import ResourceDetailOptions, load_instrument_detail
from app.utils.auth_utils import (
    authorize_user, maintainer_affiliation_filter, owner_affiliation_filter,
    require_permission, resource_capabilities)
from app.services.resource_pagination import (
    ResourcePaginationSpec, execute_resource_page, prepare_resource_pagination)
from app.utils.deletion_utils import has_deletion_blockers
from app.utils.instrument_utils import assert_instrument_id_available, get_instrument_or_404
from app.utils.permissions import AccessGroupType, Permission, Principal, Role
from app.utils.search_utils import (
    apply_trigram_search,
    configure_trigram_search,
    normalize_search_query,
    trigram_rank,
)
from app.utils.sql_utils import add_or_update, create_standard_acls, touch_resource_modification_time, deletion_filter_for
from app.utils.user_utils import resolve_create_owner, resolve_service_account_reference
from app.utils.gcs_record_utils import write_record, instrument_record

router = APIRouter(tags=["instruments"])
engine = get_engine()

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)

INSTRUMENT_PAGINATION = ResourcePaginationSpec(
    kind="instrument",
    unique_id_column=Instrument.unique_id,
    sorts={
        "instrument_id": func.lower(Instrument.instrument_id),
        "name": func.lower(Instrument.instrument_name),
        "created": Resource.creation_time,
        "updated": func.coalesce(Resource.modification_time, Resource.creation_time),
        "activity": Instrument.last_activity_at,
    },
    default_sort="created",
    default_direction="desc",
)

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


VALID_INSTRUMENT_STATUSES = tuple(status.value for status in InstrumentStatus)


def _instrument_affiliation_filter(principal, affiliation):
    predicates = {
        InstrumentAffiliation.OWNER: owner_affiliation_filter,
        InstrumentAffiliation.MAINTAINER: maintainer_affiliation_filter,
    }
    return or_(*(predicates[value](principal, Instrument) for value in dict.fromkeys(affiliation)))


def _instrument_capabilities(session, principal, instrument):
    return resource_capabilities(
        session,
        principal,
        instrument.unique_id,
        can_delete=lambda: not has_deletion_blockers(session, "instrument", instrument.unique_id),
    )


def _instrument_response(instrument, resource=None, scientific_metadata=None, owner=None,
                         capabilities=None, members=None, role=None):
    return InstrumentResponse.model_validate({
        **instrument.model_dump(exclude={"owner"}),
        "owner_orcid": instrument.owner,
        "owner": owner,
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": scientific_metadata,
        "capabilities": capabilities,
        "members": members,
        "role": role,
    })


def _instrument_roles(session, principal, instrument_mfids) -> dict:
    """Caller's effective role on each instrument record, from one ACL query."""
    group_ids = {m.id for m in principal.groups}
    if not instrument_mfids or not group_ids:
        return {}
    acl_by_mfid: dict = {}
    for mfid, group_id, permission in session.exec(
        select(ResourceACL.mfid, ResourceACL.accessgroup_id, ResourceACL.permission)
        .where(ResourceACL.mfid.in_(instrument_mfids), ResourceACL.accessgroup_id.in_(group_ids))
    ).all():
        if permission is not None:
            acl_by_mfid.setdefault(mfid, []).append((group_id, Role.from_db(permission)))
    return {
        mfid: role.db_value
        for mfid, acl_rows in acl_by_mfid.items()
        if (role := record_role(principal, acl_rows)) is not None
    }


def _instrument_members(session, principal, instrument):
    if not may_view_members(session, principal, instrument.unique_id):
        return None
    return with_grant_capabilities(session, principal, instrument.unique_id, load_member_grants(session, instrument.unique_id))


def _instrument_owners(session, instruments):
    owner_ids = {instrument.owner for instrument in instruments if instrument.owner}
    if not owner_ids:
        return {}
    return {
        user.unique_id: UserPublicRead.model_validate(user)
        for user in session.exec(select(User).where(User.unique_id.in_(owner_ids))).all()
    }

@router.get("/instruments", response_model=PaginatedResponse[InstrumentResponse])
def list_instruments(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[InstrumentListQuery, Query()],
    request: Request = None,
):
    '''
    Returns a paginated list of instruments filtered by any provided query parameters that
    match Instrument attributes. Matching is case-insensitive exact match.
    Use instrument_id for exact slug lookup. Other filterable fields include
    other_id, other_id_source, instrument_name, manufacturer,
    model, owner, location, description, instrument_type, status.
    Use owner_id for an exact owner match; owner remains as a deprecated alias.
    Pass affiliation=owner to return only instruments authoritatively owned by the caller.
    Pass sort=instrument_id|name|created|updated with direction=asc|desc to change the order.
    Each item carries the caller's effective role on the instrument record in role.
    Pass include_total=false to omit the exact authorization-filtered count.
    Defaults to status=active; pass ?status=maintenance or ?status=decommissioned to see others.
    Exact instrument_id and mfid lookups search every status unless status is also provided.
    '''
    param_values = params.model_dump()
    instrument_id = params.instrument_id
    include_metadata = params.include_metadata
    include_owner = params.include_owner
    include_total = params.include_total
    limit = params.limit
    cursor = params.cursor
    offset = params.offset
    param_values["owner"] = params.owner_id or param_values["owner"]
    if instrument_id is None and params.mfid is None and params.status is None:
        param_values["status"] = "active"
    filters = [
        func.lower(getattr(Instrument, key)) == value.lower().strip()
        for key, value in param_values.items()
        if key in INSTRUMENT_FILTER_FIELDS
        and value is not None
    ]
    if instrument_id is not None:
        filters.append(func.lower(Instrument.instrument_id) == instrument_id.lower().strip())
    if params.mfid is not None:
        filters.append(Instrument.unique_id.in_(params.mfid))
    pagination = prepare_resource_pagination(
        params=params,
        query_params=request.query_params if request is not None else {},
        spec=INSTRUMENT_PAGINATION,
        result_filters=params.normalized_result_filters(),
        user_id=auth_res.user_id,
    )

    with Session(engine) as session:
        if include_metadata:
            statement = (
                select(Instrument, Resource, ScientificMetadataBinary)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Instrument.unique_id, isouter=True)
            )
        else:
            statement = select(Instrument, Resource).join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
        statement = statement.where(deletion_filter_for(Instrument))
        if params.active_since is not None:
            statement = statement.where(Instrument.last_activity_at >= params.active_since)
        for f in filters:
            statement = statement.where(f)
        if params.affiliation:
            statement = statement.where(_instrument_affiliation_filter(auth_res, params.affiliation))

        total = (
            session.exec(select(func.count()).select_from(statement.with_only_columns(Instrument.id).subquery())).one()
            if include_total is not False
            else None
        )
        page = execute_resource_page(session, statement, pagination)
        rows = page.rows
        owners = _instrument_owners(session, (row[0] for row in rows)) if include_owner else {}
        roles = _instrument_roles(session, auth_res, [row[0].unique_id for row in rows])

        items = []
        for row in rows:
            inst, ri = row[0], row[1]
            smb = row[2] if include_metadata else None
            items.append(_instrument_response(
                inst,
                ri,
                smb.scientific_metadata if smb else None,
                owners.get(inst.owner),
                _instrument_capabilities(session, auth_res, inst) if instrument_id is not None else None,
                _instrument_members(session, auth_res, inst) if params.include_members else None,
                role=roles.get(inst.unique_id),
            ))
        return PaginatedResponse(
            total=total,
            limit=limit,
            offset=offset if cursor is None else None,
            items=items,
            next_cursor=page.next_cursor,
        )


@router.get("/instruments/search", response_model=PaginatedResponse[InstrumentResponse])
def search_instruments(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[InstrumentSearchQuery, Query()],
):
    '''
    Search instruments by name, type, or manufacturer using fuzzy matching.
    Returns results ordered by match quality. Maximum 50 results.
    Use owner_id for an exact owner match.
    '''
    normalized_q, tokens = normalize_search_query(params.q)
    with Session(engine) as session:
        configure_trigram_search(session, get_trgm_search_threshold())
        base = (
            select(Instrument, Resource)
            .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
        )
        base = base.where(deletion_filter_for(Instrument))
        if params.status is not None:
            base = base.where(Instrument.status == params.status)
        if params.affiliation:
            base = base.where(_instrument_affiliation_filter(auth_res, params.affiliation))
        if params.owner_id is not None:
            base = base.where(Instrument.owner == params.owner_id)
        fields = (
            Instrument.instrument_name,
            Instrument.instrument_type,
            Instrument.manufacturer,
        )
        base = apply_trigram_search(
            base,
            tokens,
            fields=fields,
            prefix_fields=(Instrument.instrument_name,),
        )
        statement = base.order_by(
            trigram_rank(normalized_q, fields).desc(),
            Instrument.unique_id.desc(),
        ).limit(params.limit)
        rows = session.exec(statement).all()
        owners = _instrument_owners(session, (inst for inst, _ in rows)) if params.include_owner else {}
        items = [
            _instrument_response(inst, ri, owner=owners.get(inst.owner))
            for inst, ri in rows
        ]
        return PaginatedResponse(limit=params.limit, items=items)


@router.get("/instruments/{instrument_ref}", response_model=InstrumentResponse)
def read_instrument(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    instrument_ref: InstrumentRef,
    params: Annotated[InstrumentDetailQuery, Query()],
    response: Response,
):
    '''
    Returns the instrument with the provided unique ID, or 404 if not found.
    The owner identity is resolved by default. Pass include_owner=false to omit it.
    '''
    with Session(engine) as session:
        instrument_mfid = get_instrument_or_404(session, instrument_ref).unique_id
        if instrument_ref != instrument_mfid:
            response.headers["Content-Location"] = f"/instruments/{instrument_mfid}"
        return load_instrument_detail(
            session,
            auth_res,
            instrument_mfid,
            options=ResourceDetailOptions(
                include_metadata=params.include_metadata,
                include_owner=params.include_owner,
                include_members=params.include_members,
            ),
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
        now = get_utc_datetime()
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

        add_or_update(session, AccessGroup,
                      AccessGroup.group_name == db_instrument.unique_id,
                      group_name=db_instrument.unique_id,
                      group_type=AccessGroupType.INSTRUMENT.value)
        create_standard_acls(
            session,
            db_instrument.unique_id,
            owner_id=owner_user.unique_id,
            requestor_id=user_unique_id,
        )

        session.commit()
        session.refresh(db_instrument)
        background_tasks.add_task(write_record, db_instrument.unique_id, instrument_record(db_instrument, ri), user_unique_id, "create")
        return _instrument_response(
            db_instrument,
            ri,
            owner=UserPublicRead.model_validate(owner_user),
            capabilities=_instrument_capabilities(session, auth_res, db_instrument),
        )


@router.patch("/instruments/{instrument_ref}", response_model=InstrumentResponse)
def update_instrument(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    instrument_ref: InstrumentRef,
    instrument_update: InstrumentUpdate,
    background_tasks: BackgroundTasks,
):
    '''
    Partially update an instrument record. Only provided fields are modified.
    '''
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        instrument = get_instrument_or_404(session, instrument_ref)
        instrument_mfid = instrument.unique_id

        require_permission(session, auth_res, instrument_mfid, Permission.EDIT)

        update_data = instrument_update.model_dump(exclude_unset=True)
        if update_data.get("instrument_id"):
            assert_instrument_id_available(session, update_data["instrument_id"],
                                           exclude_unique_id=instrument_mfid)

        for key, value in update_data.items():
            setattr(instrument, key, value)

        ri = session.exec(select(Resource).where(Resource.mfid == instrument_mfid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_datetime()

        session.add(instrument)
        session.commit()
        session.refresh(instrument)
        background_tasks.add_task(write_record, instrument.unique_id, instrument_record(instrument, ri), user_unique_id, "update")
        return _instrument_response(
            instrument,
            ri,
            capabilities=_instrument_capabilities(session, auth_res, instrument),
        )


@router.post("/instruments/{instrument_ref}/status", response_model=InstrumentResponse)
def set_instrument_status(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    instrument_ref: InstrumentRef,
    status: str,
    background_tasks: BackgroundTasks,
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
        instrument = get_instrument_or_404(session, instrument_ref)
        instrument_mfid = instrument.unique_id

        # admin, not editor: retiring a machine is not a metadata edit, and the
        # instrument's own service account holds only editor.
        require_permission(session, auth_res, instrument_mfid, Permission.DELETE)

        instrument.status = status
        session.add(instrument)
        touch_resource_modification_time(session, instrument_mfid)
        session.commit()
        session.refresh(instrument)

        ri = session.exec(select(Resource).where(
            Resource.mfid == instrument_mfid)).one_or_none()
        background_tasks.add_task(write_record, instrument.unique_id, instrument_record(instrument, ri), auth_res.user_id, "update")
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


@router.get("/instruments/{instrument_ref}/service_accounts", response_model=List[AccessGroupMemberRead])
def list_instrument_service_accounts(
    instrument_ref: InstrumentRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    with Session(engine) as session:
        instrument = get_instrument_or_404(session, instrument_ref)
        instrument_mfid = instrument.unique_id

        require_permission(session, auth_res, instrument_mfid, Permission.MANAGE_ACL)
        return _get_instrument_operators(instrument_mfid, session)


@router.post("/instruments/{instrument_ref}/service_accounts/{service_account_ref}", response_model=List[AccessGroupMemberRead])
def add_instrument_service_account(
    instrument_ref: InstrumentRef,
    service_account_ref: ServiceAccountRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''
    Bind a service account to an instrument's operator group, at admin standing.
    Requires admin on the instrument.
    '''
    with Session(engine) as session:
        instrument = get_instrument_or_404(session, instrument_ref)
        instrument_mfid = instrument.unique_id

        require_permission(session, auth_res, instrument_mfid, Permission.MANAGE_ACL)

        sa = resolve_service_account_reference(session, service_account_ref)

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


@router.delete("/instruments/{instrument_ref}/service_accounts/{service_account_ref}", response_model=List[AccessGroupMemberRead])
def remove_instrument_service_account(
    instrument_ref: InstrumentRef,
    service_account_ref: ServiceAccountRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''
    Unbind a service account from an instrument's operator group.
    Requires admin on the instrument.
    '''
    with Session(engine) as session:
        instrument = get_instrument_or_404(session, instrument_ref)
        instrument_mfid = instrument.unique_id

        require_permission(session, auth_res, instrument_mfid, Permission.MANAGE_ACL)

        sa = resolve_service_account_reference(session, service_account_ref)

        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == instrument_mfid)).one_or_none()
        link = session.exec(
            select(UserAccessGroupLink)
            .where(UserAccessGroupLink.user_id == sa.id, UserAccessGroupLink.accessgroup_id == ag.id)
        ).one_or_none() if ag else None
        if link is None:
            raise HTTPException(
                status_code=404,
                detail=f"'{sa.unique_id}' is not bound to instrument '{instrument_mfid}'")
        session.delete(link)
        session.commit()

        return _get_instrument_operators(instrument_mfid, session)
