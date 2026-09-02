from sqlmodel import SQLModel

from typing import Optional

from app.models.users import UserPublicRead


class AccessGroupCreate(SQLModel):
    '''Fields required to create a new access group.'''
    group_name: str


class AccessGroupRead(SQLModel):
    '''Response model for AccessGroup.'''
    id: int
    group_name: str


class AccessGroupMemberRead(UserPublicRead):
    '''A public-safe group member profile plus their standing role.'''
    is_service_account: Optional[bool] = False
    role: str
