from typing import Any, Optional
from pydantic import model_validator
from sqlmodel import SQLModel


class UserCreate(SQLModel):
    '''Input schema for creating a user.'''
    first_name: str
    last_name: str
    unique_id: str
    email: Optional[str] = None
    is_service_account: Optional[bool] = False


class UserRead(SQLModel):
    first_name: str
    last_name: str
    email: Optional[str] = None
    unique_id: str
    is_service_account: bool = False


class UserUpdate(SQLModel):
    '''Partial update schema for User. Only provided fields are modified.'''
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    is_service_account: Optional[bool] = None
