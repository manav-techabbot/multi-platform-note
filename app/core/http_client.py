import httpx

from app.config import Settings

# Tumblr requires a consistent User-Agent, and DEV rejects requests that send none.
USER_AGENT = "automated-posting/1.0"


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout),
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=False,
    )
