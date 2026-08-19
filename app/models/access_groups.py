from sqlmodel import SQLModel

from app.models.users import UserPublicRead, UserRead


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


class AccessGroupMemberPublicRead(UserPublicRead):
    '''Public-safe (no email) member profile plus their standing role in
    that group. Used wherever a member list is shown to an audience wider
    than the group's own members/admins.'''
    role: str
