"""Programs that sign people in through this API: CRUCIBLE_AUTH_CLIENTS, or the Hub alone from the
older HUB_AUTH_* settings when that list is not given."""
import logging
import secrets
from dataclasses import dataclass

from urllib.parse import urlsplit

from app.config import LOOPBACK_HOSTS, ConfigurationError, get_settings, optional_secret

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    display_name: str
    redirect_uris: tuple[str, ...]
    secret_name: str | None
    type: str = "confidential"

    @property
    def is_public(self) -> bool:
        return self.type == "public"

    def allows_redirect(self, uri: str) -> bool:
        """Exact match, except that a public client's loopback entries accept any port (RFC 8252 7.3)."""
        if uri in self.redirect_uris:
            return True
        if not self.is_public:
            return False
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS or port is None or parsed.query or parsed.fragment or parsed.username:
            return False
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return f"http://{host}{parsed.path}" in self.redirect_uris


def registered_clients() -> tuple[RegisteredClient, ...]:
    settings = get_settings()
    if settings.auth.clients:
        listed = tuple(RegisteredClient(client.client_id, client.display_name, client.redirect_uris, client.secret_name if client.type == "confidential" else None, client.type) for client in settings.auth.clients)
        return listed + _device_approval(listed)
    hub = settings.hub
    if not hub.enabled:
        return ()
    redirects = tuple(uri.strip() for uri in hub.callback_uri.split(",") if uri.strip())
    return (RegisteredClient(hub.client_id, "Crucible Hub", redirects, "HUB_AUTH_CLIENT_SECRET"),)


def _device_approval(listed: tuple[RegisteredClient, ...]) -> tuple[RegisteredClient, ...]:
    """The API's own page for approving device sign-ins signs people in like a client, with its own
    callback, but never receives tokens. It exists only when a public client may use the device flow."""
    from app.auth.devices import INTERNAL_CLIENT_ID
    from app.utils.hub_login import HubLoginUnavailable, public_api_url

    if not any(client.is_public for client in listed):
        return ()
    try:
        callback = f"{public_api_url()}/oauth/device/callback"
    except (HubLoginUnavailable, ConfigurationError):
        return ()
    return (RegisteredClient(INTERNAL_CLIENT_ID, "Crucible", (callback,), None, "internal"),)


def sign_in_enabled() -> bool:
    return bool(registered_clients())


def client_by_id(client_id: str | None) -> RegisteredClient | None:
    return next((client for client in registered_clients() if client.client_id == client_id), None)


class ClientUnavailable(Exception):
    """A registered client's secret is missing or cannot be read: the server is misconfigured."""


def require_client_secrets() -> None:
    """Raise ClientUnavailable unless every registered client's secret can be read."""
    for client in registered_clients():
        if client.secret_name is None:
            continue
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
    if client is not None and client.secret_name is None:
        client = None
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
