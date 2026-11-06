HUB_TEST_CALLBACK = "https://crucible.lbl.gov/hub/auth/callback"


def hub_login_config(callback: str = HUB_TEST_CALLBACK, api_url: str = "https://crucible.lbl.gov/api/v3"):
    from app.auth.clients import RegisteredClient
    from app.utils.hub_login import HubLoginConfig

    return HubLoginConfig((RegisteredClient("crucible-hub", "Crucible Hub", (callback,), "HUB_AUTH_CLIENT_SECRET"),), api_url)
