from typing import Optional, List
from sqlmodel import Field, SQLModel,Relationship

class UserProposalLink(SQLModel, table=True):
    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", primary_key=True
    )
    proposal_id: Optional[str] = Field(
        default=None, foreign_key="proposal.id", primary_key=True
    )


class UserAccessGroupLink(SQLModel, table=True):
    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", primary_key=True
    )
    accessgroup_id: Optional[int] = Field(
        default=None, foreign_key="accessgroup.id", primary_key=True
    )


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    first_name: str
    last_name: str
    email: Optional[str]
    lbl_email: Optional[str]
    orcid: str = Field(index=True, unique=True)
    employee_number: Optional[str]
    proposals: List["Proposal"] = Relationship(back_populates="users", link_model=UserProposalLink)
    accessgroups: List["AccessGroup"] = Relationship(back_populates="users", link_model=UserAccessGroupLink)


class Proposal(SQLModel, table=True):
    id: str = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    organization: str 
    status: Optional[str]
    title: Optional[str]
    assigned_scientist_name: Optional[str]
    assigned_scientist_email: str
    users: List[User] = Relationship(back_populates="proposals", link_model=UserProposalLink)


class AccessGroup(SQLModel, table=True):
    from app.models.dataset import DatasetACL
    id: Optional[int] = Field(default=None, primary_key=True)
    group_name: str = Field(index=True, unique=True)
    users: List[User] = Relationship(back_populates="accessgroups", link_model=UserAccessGroupLink)
    #datasets: List[Dataset] = Relationship(back_populates="accessgroups", link_model=DatasetAccessGroupLink)
    acl: List["DatasetACL"] = Relationship(back_populates="accessgroup")
