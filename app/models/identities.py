from typing import Literal, Optional

from pydantic import Field
from sqlmodel import SQLModel

from app.models.timestamps import UtcDatetime


class IdentityRead(SQLModel):
    '''One way the person signs in. Only an ORCID shows its identifier, since ORCIDs are public.'''
    id: int
    provider_key: Optional[str] = Field(default=None, description="The provider's key from GET /auth/methods; null when the provider is no longer listed.")
    provider_name: str
    label: Optional[str] = Field(default=None, description="What the provider calls this login, such as an email address; for an ORCID, the ORCID.")
    orcid: Optional[str] = None
    verified_at: Optional[UtcDatetime] = Field(default=None, description="When the person last proved this login by signing in with it; null when an administrator entered it.")
    created_at: UtcDatetime
    last_used_at: Optional[UtcDatetime] = None


class LinkStart(SQLModel):
    callback_uri: str = Field(min_length=1, max_length=512)
    state: str = Field(min_length=16, max_length=256)
    code_challenge: str = Field(min_length=43, max_length=43)


class LinkStartRead(SQLModel):
    authorization_url: str = Field(description="A one-time address on the API. Send the person's browser there; it continues to the provider.")


class LinkCode(SQLModel):
    code: str = Field(min_length=1, max_length=128)
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=512)
    session_token: str = Field(min_length=1, max_length=256, description="The access token of the Hub session confirming the link; it must belong to the person who started it.")


class LinkPreviewRead(SQLModel):
    provider_key: Optional[str] = None
    provider_name: str
    label: Optional[str] = None
    orcid: Optional[str] = None
    status: Literal["available", "already_yours", "linked_elsewhere", "second_orcid", "orcid_of_another"] = Field(
        description="available and already_yours can be confirmed; already_yours marks the login verified. The others are refused."
    )


class PasswordLinkRead(SQLModel):
    """A one-time link to set or reset a password, shown only in this response."""
    url: str
    purpose: str = Field(description="`setup` when the person has no password yet, `reset` when they have one")
    expires_at: UtcDatetime


class SessionRead(SQLModel):
    """One signed-in session. Never its tokens."""
    id: str
    client_id: str
    client_name: str
    created_at: UtcDatetime
    last_used_at: UtcDatetime = Field(description="When it last got new tokens (it refreshes about every 15 minutes while in use)")
    expires_at: UtcDatetime
    signed_in_at: UtcDatetime | None = Field(default=None, description="When the person last signed in for it")
    current: bool = Field(default=False, description="The session making this request")
