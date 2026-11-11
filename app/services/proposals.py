import logging

import requests
from sqlmodel import Session, select

from app.config import ConfigurationError, get_secret, get_settings
from app.tables import AccessGroup, Project, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, Role
from app.utils.project_utils import resolve_project_reference
from app.utils.sql_utils import get_active_deletion_request
from app.utils.user_utils import sql_add_user_to_access_group

logger = logging.getLogger(__name__)

MF_PROPOSAL_DB_TIMEOUT = 10


class ProposalServiceUnavailable(Exception):
    pass


def lookup_proposals(orcid: str) -> list[str]:
    """The MF proposal IDs for an ORCID; nothing when proposal sync is off. Raises
    ProposalServiceUnavailable when the service cannot answer."""
    if not get_settings().proposals.enabled:
        return []
    try:
        api_key = get_secret("PROPDB_APIKEY")
    except ConfigurationError as exc:
        logger.warning("Could not load proposal service credentials: %s", exc)
        raise ProposalServiceUnavailable from None
    if not api_key:
        logger.warning("Proposal service credentials are unavailable")
        raise ProposalServiceUnavailable

    try:
        response = requests.get(
            get_settings().proposals.url,
            params={"key": api_key, "orcid": orcid},
            timeout=MF_PROPOSAL_DB_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("MF proposal lookup failed for %s: %s%s", orcid, type(exc).__name__, f" {status}" if status else "")
        raise ProposalServiceUnavailable from None

    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        logger.warning("MF proposal lookup returned an invalid response for %s", orcid)
        raise ProposalServiceUnavailable
    if any(not isinstance(item.get("proposals", []), list) for item in payload):
        logger.warning("MF proposal lookup returned invalid proposals for %s", orcid)
        raise ProposalServiceUnavailable
    return sorted({
        proposal
        for item in payload
        for proposal in item.get("proposals", [])
        if isinstance(proposal, str)
    })


def find_mf_user_proposals(orcid: str) -> list[str]:
    """For creating a person, which carries on without the lookup when the service is down."""
    try:
        return lookup_proposals(orcid)
    except ProposalServiceUnavailable:
        return []


def usable_discovered_projects(session: Session, proposal_ids: list[str]) -> tuple[list[Project], list[str]]:
    """The projects a person may join because a proposal names them, and the proposal IDs that
    cannot be used: not in Crucible, with proposal sync off, pending deletion, or with no access group."""
    usable, unusable = [], []
    for proposal_id in sorted({proposal_id for proposal_id in proposal_ids if proposal_id}):
        project = resolve_project_reference(proposal_id, session)
        if project is None:
            logger.warning("Ignoring proposal project not provisioned in Crucible: %s", proposal_id)
        elif not project.proposal_sync:
            logger.warning("Ignoring project with proposal sync off: %s", proposal_id)
            project = None
        elif get_active_deletion_request(session, project.unique_id) is not None:
            logger.warning("Ignoring proposal project pending deletion: %s", proposal_id)
            project = None
        elif session.exec(select(AccessGroup.id).where(AccessGroup.group_name == project.unique_id, AccessGroup.group_type == AccessGroupType.PROJECT.value)).first() is None:
            logger.warning("Ignoring proposal project with no access group: %s", proposal_id)
            project = None
        if project is None:
            unusable.append(proposal_id)
        else:
            usable.append(project)
    return usable, unusable


def assign_discovered_projects(session: Session, user: User, proposal_ids: list[str]) -> dict[str, list[str]]:
    """Add memberships without removing any, in the caller's transaction."""
    projects, not_found = usable_discovered_projects(session, proposal_ids)
    added, already_member = [], []
    for project in projects:
        member = session.exec(
            select(UserAccessGroupLink.user_id)
            .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
            .where(UserAccessGroupLink.user_id == user.id, AccessGroup.group_name == project.unique_id)
        ).first()
        (already_member if member is not None else added).append(project.project_id)
        sql_add_user_to_access_group(user.unique_id, project.unique_id, session, role=Role.CONTRIBUTOR, group_type=AccessGroupType.PROJECT)
    return {"projects_added": added, "projects_already_member": already_member, "projects_not_found": not_found}
