from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.config import Settings
from app.core.http_client import create_http_client

MESSAGE = "Check out our latest business platform. https://example.com"
CTA_URL = "https://example.com"
REQUEST_ID = "test-request-id"

API_URLS = {
    "devto_base_url": "https://dev.to",
    "bluesky_service_url": "https://bsky.social",
    "bluesky_web_url": "https://bsky.app",
    "tumblr_api_base_url": "https://api.tumblr.com/v2",
    "tumblr_authorize_url": "https://www.tumblr.com/oauth2/authorize",
    "tumblr_token_url": "https://api.tumblr.com/v2/oauth2/token",
    "tumblr_redirect_uri": "http://localhost:4000/tumblr/callback",
    "github_api_base_url": "https://api.github.com",
    "jsonplaceholder_base_url": "https://jsonplaceholder.typicode.com",
}

SECRETS = {
    "devto_api_key": "devto-secret-key",
    "bluesky_app_password": "bluesky-secret-password",
    "tumblr_secret_key": "tumblr-secret-client",
    "tumblr_access_token": "tumblr-secret-access",
    "tumblr_refresh_token": "tumblr-secret-refresh",
    "github_gist_token": "github-secret-token",
}


def make_settings(**overrides) -> Settings:
    values = {
        **API_URLS,
        "bluesky_handle": "tester.bsky.social",
        "tumblr_blog_identifier": "testblog",
        "tumblr_consumer_key": "tumblr-client-id",
        "github_api_version": "2026-03-10",
        **SECRETS,
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def empty_settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
async def http_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with create_http_client(settings) as client:
        yield client


@pytest.fixture
def mock_api() -> AsyncIterator[respx.MockRouter]:
    # Any request that is not explicitly mocked fails the test instead of reaching the network.
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture(autouse=True)
def isolated_token_store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # Settings read this from the environment, so no test can touch a real token file.
    path = tmp_path / "secrets" / "tumblr_tokens.json"
    monkeypatch.setenv("TUMBLR_TOKEN_STORE_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("app.core.retry.asyncio.sleep", record_delay)
    return delays
