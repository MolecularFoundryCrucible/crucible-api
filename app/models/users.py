from typing import Optional
from sqlmodel import SQLModel


class UserRead(SQLModel):
    '''Response model for User.'''
    id: int
    first_name: str
    last_name: str
    email: Optional[str] = None
    orcid: str
    employee_number: Optional[str] = None


class UserUpdate(SQLModel):
    '''Partial update schema for User. Only provided fields are modified.'''
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    employee_number: Optional[str] = None
