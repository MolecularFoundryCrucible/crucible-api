from typing import Any, Protocol

from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session

from app.models.core_models import Resource
from app.utils.auth_utils import (
    access_selector_filter,
    resource_affiliation_filter,
    project_scope_filter,
)
from app.utils.permissions import Principal
from app.utils.sql_utils import deletion_filter_for, visibility_filter_for


class ResourceScopeParams(Protocol):
    accessible_to_user: list[str] | None
    accessible_to_project: list[str] | None
    affiliation: list | None
    visibility: str
    project_scope: Any

    def normalized_time(self, field_name: str): ...


def resource_scope_predicates(
    session: Session,
    principal: Principal,
    model,
    params: ResourceScopeParams,
    project_mfid: str | None,
) -> tuple[ColumnElement[bool], ...]:
    predicates = [
        access_selector_filter(
            session,
            principal,
            model,
            user_refs=params.accessible_to_user,
            project_refs=params.accessible_to_project,
        ),
        deletion_filter_for(model),
    ]
    if params.affiliation:
        predicates.append(resource_affiliation_filter(principal, model, params.affiliation))
    if params.visibility != "all":
        predicates.append(visibility_filter_for(model, params.visibility))
    for parameter, column, comparison in (
        ("creation_time_gte", Resource.creation_time, lambda left, right: left >= right),
        ("creation_time_lte", Resource.creation_time, lambda left, right: left <= right),
        ("modification_time_gte", Resource.modification_time, lambda left, right: left >= right),
        ("modification_time_lte", Resource.modification_time, lambda left, right: left <= right),
    ):
        value = params.normalized_time(parameter)
        if value is not None:
            predicates.append(comparison(column, value))
    if project_mfid is not None:
        predicates.append(
            project_scope_filter(
                session,
                principal,
                model,
                project_mfid,
                params.project_scope or "assigned",
            )
        )
    return tuple(predicates)
