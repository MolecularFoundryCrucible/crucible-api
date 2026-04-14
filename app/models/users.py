from typing import Any, Optional
from pydantic import model_validator
from sqlmodel import SQLModel


class UserCreate(SQLModel):
    '''Input schema for creating a user.'''
    first_name: str
    last_name: str
    orcid: str
    email: Optional[str] = None
    lbl_email: Optional[str] = None


class UserRead(SQLModel):
    '''Response model for User. email returns lbl_email if populated, else email.'''
    # id: int
    first_name: str
    last_name: str
    email: Optional[str] = None
    orcid: str
    # employee_number: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def prefer_lbl_email(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data['email'] = data.get('lbl_email') or data.get('email')
        else:
            lbl = getattr(data, 'lbl_email', None)
            if lbl:
                return {
                    # 'id': data.id,
                    'first_name': data.first_name,
                    'last_name': data.last_name,
                    'email': lbl,
                    'orcid': data.orcid,
                    # 'employee_number': getattr(data, 'employee_number', None),
                }
        return data


class UserUpdate(SQLModel):
    '''Partial update schema for User. Only provided fields are modified.'''
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    # employee_number: Optional[str] = None
