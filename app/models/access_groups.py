from typing import Optional

from app.models.access import EntryCapabilities
from app.models.users import UserPublicRead


class AccessGroupMemberRead(UserPublicRead):
    '''A public-safe group member profile plus their standing role.'''
    is_service_account: Optional[bool] = False
    role: str
    capabilities: Optional[EntryCapabilities] = None
