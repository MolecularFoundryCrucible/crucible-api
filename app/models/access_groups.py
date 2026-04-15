from sqlmodel import SQLModel


class AccessGroupCreate(SQLModel):
    '''Fields required to create a new access group.'''
    group_name: str


class AccessGroupRead(SQLModel):
    '''Response model for AccessGroup.'''
    id: int
    group_name: str
