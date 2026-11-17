from typing import Literal, Optional

from pydantic import Field
from sqlmodel import SQLModel

from app.models.timestamps import UtcDatetime


class ApiKeyCreate(SQLModel):
    name: str = Field(min_length=1, max_length=100, description="What the key is for, such as the machine or script that uses it.")
    expires_at: Optional[UtcDatetime] = Field(default=None, description="When the key stops working, at most a year from now; a year when omitted.")


class ApiKeyRename(SQLModel):
    name: str = Field(min_length=1, max_length=100)


class ApiKeyListItem(SQLModel):
    id: int
    name: Optional[str] = None
    last4: str = Field(description="The key's last four characters, to recognise it. The key itself is never shown again.")
    status: Literal["active", "expired", "revoked"]
    created_at: UtcDatetime
    last_used_at: Optional[UtcDatetime] = None
    expires_at: Optional[UtcDatetime] = None
    revoked_at: Optional[UtcDatetime] = None
    older_routes: bool = Field(default=False, deprecated=True, description="Deprecated: always false, since the single-key routes are gone. Removed in a later release.")


class ApiKeyCreated(ApiKeyListItem):
    api_key: str = Field(description="The key. Shown only in this response; store it now.")
