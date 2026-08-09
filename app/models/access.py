from typing import Optional

from sqlmodel import SQLModel


class AccessGrantRead(SQLModel):
    """One principal's access to a resource.

    `principal` is what a caller would type — an ORCID, a project_id, or
    `public` — rather than the underlying group name, which is an internal
    detail and is due to become an MFID.
    """
    principal: str
    kind: str            # user | service_account | project | instrument | public | unknown
    role: str
    display_name: Optional[str] = None
    group_name: str      # the group actually holding the grant
    group_id: int


class AccessGrantWrite(SQLModel):
    """Body for PUT /resources/{mfid}/access/{kind}/{principal}."""
    role: str
