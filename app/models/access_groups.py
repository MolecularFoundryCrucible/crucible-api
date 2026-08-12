from sqlmodel import SQLModel

from app.models.users import UserRead


class AccessGroupCreate(SQLModel):
    '''Fields required to create a new access group.'''
    group_name: str


class AccessGroupRead(SQLModel):
    '''Response model for AccessGroup.'''
    id: int
    group_name: str


class AccessGroupMemberRead(UserRead):
    '''A group member's profile plus their standing role in that group.'''
    role: str
