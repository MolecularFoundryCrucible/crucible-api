"""Programs that sign people in through this API: CRUCIBLE_AUTH_CLIENTS, or the Hub alone from the
older HUB_AUTH_* settings when that list is not given."""
import logging
import secrets
from dataclasses import dataclass

from app.config import ConfigurationError, get_settings, optional_secret

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    display_name: str
    redirect_uris: tuple[str, ...]
    secret_name: str


def registered_clients() -> tuple[RegisteredClient, ...]:
    settings = get_settings()
    if settings.auth.clients:
        return tuple(RegisteredClient(client.client_id, client.display_name, client.redirect_uris, client.secret_name) for client in settings.auth.clients)
    hub = settings.hub
    if not hub.enabled:
        return ()
    redirects = tuple(uri.strip() for uri in hub.callback_uri.split(",") if uri.strip())
    return (RegisteredClient(hub.client_id, "Crucible Hub", redirects, "HUB_AUTH_CLIENT_SECRET"),)


def sign_in_enabled() -> bool:
    return bool(registered_clients())


def client_by_id(client_id: str | None) -> RegisteredClient | None:
    return next((client for client in registered_clients() if client.client_id == client_id), None)


class ClientUnavailable(Exception):
    """A registered client's secret is missing or cannot be read: the server is misconfigured."""


def require_client_secrets() -> None:
    """Raise ClientUnavailable unless every registered client's secret can be read."""
    for client in registered_clients():
        try:
            present = optional_secret(client.secret_name)
        except ConfigurationError:
            logger.warning("Client secret %s cannot be read", client.secret_name)
            raise ClientUnavailable from None
        if not present:
            raise ClientUnavailable


def authenticate_client(client_id: str, presented_secret: str) -> RegisteredClient | None:
    """The client these HTTP Basic credentials name, if its secret matches. The comparison runs even
    for an unknown client, so the time taken does not tell which client IDs exist."""
    client = client_by_id(client_id)
    expected = None
    if client is not None:
        try:
            expected = optional_secret(client.secret_name)
        except ConfigurationError:
            logger.warning("Client secret %s cannot be read", client.secret_name)
            raise ClientUnavailable from None
        if not expected:
            raise ClientUnavailable
    matches = secrets.compare_digest(presented_secret.encode("utf-8"), (expected or secrets.token_urlsafe(32)).encode("utf-8"))
    return client if matches and expected else None
