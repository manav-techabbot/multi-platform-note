"""Tumblr OAuth 2.0: token storage, state handling, token exchange, refresh and routes.

Every Tumblr endpoint is mocked; no real credentials are used and nothing is published.
"""

import asyncio
import json
import logging
import os
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.exceptions import AuthenticationError, NetworkError, UnexpectedResponseError
from app.main import create_app
from app.oauth.state import OAuthStateStore
from app.oauth.token_store import OAuthTokens, TokenStore
from app.oauth.tumblr import TumblrOAuthClient, TumblrTokenManager, create_token_manager
from app.publishers.tumblr import TumblrPublisher
from app.schemas import ResultStatus
from tests.conftest import MESSAGE, SECRETS, make_settings
from tests.platforms.test_tumblr import CREATED, FETCHED, POSTS_URL

REQUEST_ID = "oauth-test"
REDIRECT_URI = "http://localhost:4000/tumblr/callback"
TOKEN_URL = "https://api.tumblr.com/v2/oauth2/token"


def token_payload(access="new-access", refresh="new-refresh", expires_in=2520) -> dict:
    payload = {
        "access_token": access,
        "expires_in": expires_in,
        "token_type": "bearer",
        "scope": "basic write offline_access",
    }
    if refresh:
        payload["refresh_token"] = refresh
    return payload


@pytest.fixture
def token_route(mock_api):
    return mock_api.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_payload()))


@pytest.fixture
def oauth_client(settings, http_client):
    return TumblrOAuthClient(settings, http_client)


def form(route) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(route.calls.last.request.content.decode()).items()}


# Token store


def test_token_store_round_trip(isolated_token_store):
    store = TokenStore(isolated_token_store)
    tokens = OAuthTokens("access", "refresh", 1_800_000_000.0, "basic write")

    store.save(tokens)

    assert store.load() == tokens
    assert json.loads(isolated_token_store.read_text())["refresh_token"] == "refresh"
    assert not list(isolated_token_store.parent.glob("*.tmp"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes do not apply on Windows")
def test_token_store_is_readable_by_owner_only(isolated_token_store):
    TokenStore(isolated_token_store).save(OAuthTokens("access", "refresh"))

    assert os.stat(isolated_token_store).st_mode & 0o777 == 0o600


def test_token_store_missing_or_corrupt_file(isolated_token_store, caplog):
    store = TokenStore(isolated_token_store)
    assert store.load() is None

    isolated_token_store.parent.mkdir(parents=True)
    isolated_token_store.write_text("{not json")

    assert store.load() is None
    assert "unreadable" in caplog.text


def test_token_response_parsing():
    tokens = OAuthTokens.from_token_response(token_payload(), previous_refresh_token="old")

    assert tokens.access_token == "new-access"
    assert tokens.refresh_token == "new-refresh"
    assert tokens.scope == "basic write offline_access"
    assert tokens.expires_at == pytest.approx(time.time() + 2520, abs=5)
    assert not tokens.is_expired()


def test_token_response_without_new_refresh_token_keeps_the_old_one():
    tokens = OAuthTokens.from_token_response(token_payload(refresh=None), previous_refresh_token="old")

    assert tokens.refresh_token == "old"


@pytest.mark.parametrize("payload", [None, [], {"token_type": "bearer"}, {"access_token": ""}])
def test_malformed_token_responses_are_rejected(payload):
    with pytest.raises(ValueError):
        OAuthTokens.from_token_response(payload)


def test_expiry_includes_a_safety_margin():
    assert OAuthTokens("a", expires_at=time.time() + 30).is_expired()
    assert not OAuthTokens("a", expires_at=time.time() + 300).is_expired()
    assert not OAuthTokens("a").is_expired()


# State


def test_state_is_random_and_single_use():
    states = OAuthStateStore()
    first, second = states.issue(), states.issue()

    assert first != second
    assert len(first) >= 40
    assert states.consume(first) is True
    assert states.consume(first) is False
    assert states.consume("forged-state") is False
    assert states.consume(second) is True


def test_expired_state_is_rejected():
    states = OAuthStateStore(ttl_seconds=0)
    state = states.issue()

    assert states.consume(state) is False


def test_pending_states_are_capped():
    states = OAuthStateStore(max_pending=3)
    issued = [states.issue() for _ in range(5)]

    assert states.consume(issued[0]) is False
    assert all(states.consume(state) for state in issued[2:])


# OAuth client


def test_authorization_url(oauth_client):
    url = urlsplit(oauth_client.authorization_url("abc123"))
    query = {key: values[0] for key, values in parse_qs(url.query).items()}

    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://www.tumblr.com/oauth2/authorize"
    assert query == {
        "client_id": "tumblr-client-id",
        "response_type": "code",
        "scope": "basic write offline_access",
        "state": "abc123",
        "redirect_uri": REDIRECT_URI,
    }


async def test_exchange_code(oauth_client, token_route):
    tokens = await oauth_client.exchange_code("the-code", REQUEST_ID)

    assert tokens.access_token == "new-access"
    assert tokens.refresh_token == "new-refresh"
    assert form(token_route) == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "client_id": "tumblr-client-id",
        "client_secret": "tumblr-secret-client",
        "redirect_uri": REDIRECT_URI,
    }
    assert token_route.calls.last.request.headers["User-Agent"] == "automated-posting/1.0"


async def test_rejected_code_is_an_authentication_error(oauth_client, token_route):
    token_route.mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "Code the-code expired"})
    )

    with pytest.raises(AuthenticationError) as caught:
        await oauth_client.exchange_code("the-code", REQUEST_ID)

    assert "invalid_grant" in caught.value.message
    assert "the-code" not in caught.value.message
    assert "tumblr-secret-client" not in caught.value.message


async def test_code_exchange_is_not_repeated_after_a_server_error(oauth_client, token_route):
    token_route.mock(return_value=httpx.Response(500))

    with pytest.raises(Exception) as caught:
        await oauth_client.exchange_code("the-code", REQUEST_ID)

    assert caught.value.category == "platform_unavailable"
    assert token_route.call_count == 1


async def test_code_exchange_is_not_repeated_after_a_read_timeout(oauth_client, token_route):
    token_route.mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(NetworkError):
        await oauth_client.exchange_code("the-code", REQUEST_ID)

    assert token_route.call_count == 1


async def test_connection_errors_are_retried(oauth_client, token_route):
    token_route.mock(
        side_effect=[httpx.ConnectError("dns"), httpx.Response(200, json=token_payload())]
    )

    tokens = await oauth_client.exchange_code("the-code", REQUEST_ID)

    assert tokens.access_token == "new-access"
    assert token_route.call_count == 2


async def test_token_network_failure(oauth_client, token_route):
    token_route.mock(side_effect=httpx.ConnectError("dns"))

    with pytest.raises(NetworkError):
        await oauth_client.refresh("refresh", REQUEST_ID)

    assert token_route.call_count == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>oops</html>"),
        httpx.Response(200, json={"token_type": "bearer"}),
        httpx.Response(200, json=["unexpected"]),
    ],
)
async def test_malformed_token_endpoint_response(oauth_client, token_route, response):
    token_route.mock(return_value=response)

    with pytest.raises(UnexpectedResponseError):
        await oauth_client.refresh("refresh", REQUEST_ID)


async def test_expired_refresh_token(oauth_client, token_route):
    token_route.mock(return_value=httpx.Response(401, json={"meta": {"status": 401, "msg": "Unauthorized"}}))

    with pytest.raises(AuthenticationError) as caught:
        await oauth_client.refresh("old-refresh", REQUEST_ID)

    assert "/tumblr/authorize" in caught.value.message
    assert "old-refresh" not in caught.value.message


async def test_refresh_request(oauth_client, token_route):
    await oauth_client.refresh("old-refresh", REQUEST_ID)

    assert form(token_route) == {
        "grant_type": "refresh_token",
        "client_id": "tumblr-client-id",
        "client_secret": "tumblr-secret-client",
        "refresh_token": "old-refresh",
    }


# Token manager


@pytest.fixture
def store(isolated_token_store):
    return TokenStore(isolated_token_store)


def manager_for(http_client, **overrides) -> TumblrTokenManager:
    return create_token_manager(make_settings(**overrides), http_client)


async def test_valid_stored_token_is_used_without_refreshing(http_client, store, token_route):
    store.save(OAuthTokens("stored-access", "stored-refresh", time.time() + 1000))

    assert await manager_for(http_client).access_token(REQUEST_ID) == "stored-access"
    assert not token_route.called


async def test_stored_tokens_take_precedence_over_environment(http_client, store):
    store.save(OAuthTokens("stored-access", "stored-refresh"))

    tokens = manager_for(http_client).current()

    assert tokens.access_token == "stored-access"
    assert tokens.refresh_token == "stored-refresh"


async def test_environment_tokens_seed_the_manager(http_client, store, token_route):
    manager = manager_for(http_client)

    assert await manager.access_token(REQUEST_ID) == "tumblr-secret-access"
    assert manager.current().refresh_token == "tumblr-secret-refresh"


async def test_expired_token_is_refreshed_and_rotation_is_persisted(http_client, store, token_route):
    store.save(OAuthTokens("expired-access", "refresh-1", time.time() - 10))
    manager = manager_for(http_client)

    assert await manager.access_token(REQUEST_ID) == "new-access"

    assert form(token_route)["refresh_token"] == "refresh-1"
    saved = store.load()
    assert saved.access_token == "new-access"
    assert saved.refresh_token == "new-refresh"


async def test_old_refresh_token_is_never_reused(http_client, store, token_route):
    token_route.mock(
        side_effect=[
            httpx.Response(200, json=token_payload("access-2", "refresh-2")),
            httpx.Response(200, json=token_payload("access-3", "refresh-3")),
        ]
    )
    store.save(OAuthTokens("access-1", "refresh-1"))
    manager = manager_for(http_client)

    await manager.refresh(REQUEST_ID, rejected_access_token="access-1")
    await manager.refresh(REQUEST_ID, rejected_access_token="access-2")

    used = [parse_qs(call.request.content.decode())["refresh_token"][0] for call in token_route.calls]
    assert used == ["refresh-1", "refresh-2"]
    assert store.load().refresh_token == "refresh-3"


async def test_restarted_manager_uses_the_rotated_token(http_client, store, token_route):
    await manager_for(http_client, tumblr_access_token="").access_token(REQUEST_ID)

    restarted = manager_for(http_client)

    assert restarted.current().refresh_token == "new-refresh"
    assert await restarted.access_token(REQUEST_ID) == "new-access"
    assert token_route.call_count == 1


async def test_concurrent_requests_share_one_refresh(http_client, store, token_route):
    store.save(OAuthTokens("expired", "refresh-1", time.time() - 10))
    manager = manager_for(http_client)

    tokens = await asyncio.gather(*(manager.access_token(REQUEST_ID) for _ in range(5)))

    assert set(tokens) == {"new-access"}
    assert token_route.call_count == 1


async def test_missing_refresh_token(http_client, store, token_route):
    manager = manager_for(http_client, tumblr_refresh_token="")

    with pytest.raises(AuthenticationError, match="TUMBLR_ACCESS_TOKEN from the environment.*no refresh token"):
        await manager.refresh(REQUEST_ID, rejected_access_token="tumblr-secret-access")

    assert not token_route.called


async def test_refresh_needs_client_credentials(http_client, store, token_route):
    manager = manager_for(http_client, tumblr_secret_key="")

    with pytest.raises(AuthenticationError, match="TUMBLR_SECRET_KEY"):
        await manager.refresh(REQUEST_ID, rejected_access_token="tumblr-secret-access")

    assert not token_route.called


async def test_no_tokens_at_all(http_client, store):
    manager = manager_for(http_client, tumblr_access_token="", tumblr_refresh_token="")

    with pytest.raises(AuthenticationError, match="/tumblr/authorize"):
        await manager.access_token(REQUEST_ID)


async def test_failed_save_keeps_new_tokens_in_memory(http_client, store, token_route, monkeypatch, caplog):
    def fail(tokens):
        raise PermissionError(13, "Permission denied")

    store.save(OAuthTokens("expired", "refresh-1", time.time() - 10))
    manager = manager_for(http_client)
    monkeypatch.setattr(manager.store, "save", fail)

    assert await manager.access_token(REQUEST_ID) == "new-access"
    assert manager.current().refresh_token == "new-refresh"
    assert "kept in memory" in caplog.text
    assert "new-refresh" not in caplog.text


# Publisher integration


async def test_publisher_refreshes_an_expired_token_before_publishing(
    http_client, store, token_route, mock_api
):
    store.save(OAuthTokens("expired-access", "refresh-1", time.time() - 10))
    create = mock_api.post(POSTS_URL).mock(return_value=httpx.Response(201, json=CREATED))
    mock_api.get(POSTS_URL).mock(return_value=httpx.Response(200, json=FETCHED))
    publisher = TumblrPublisher(make_settings(), http_client)

    result = await publisher.publish(MESSAGE, "https://example.com", REQUEST_ID)

    assert result.status is ResultStatus.SUCCESS
    assert create.call_count == 1
    assert create.calls.last.request.headers["Authorization"] == "Bearer new-access"
    assert store.load().refresh_token == "new-refresh"


async def test_publisher_masks_stored_tokens_in_errors(http_client, store, mock_api):
    store.save(OAuthTokens("stored-access-xyz", "stored-refresh-xyz"))
    mock_api.post(POSTS_URL).mock(
        return_value=httpx.Response(400, json={"errors": [{"detail": "bad token stored-access-xyz"}]})
    )

    result = await TumblrPublisher(make_settings(), http_client).publish(MESSAGE, "https://example.com", REQUEST_ID)

    assert "stored-access-xyz" not in result.error.message
    assert "***" in result.error.message


async def test_authorized_store_alone_configures_publishing(http_client, store):
    store.save(OAuthTokens("stored-access", "stored-refresh"))
    publisher = TumblrPublisher(make_settings(tumblr_access_token="", tumblr_refresh_token=""), http_client)

    assert publisher.missing_settings() == []


async def test_missing_tokens_point_to_authorization(http_client, store):
    publisher = TumblrPublisher(make_settings(tumblr_access_token="", tumblr_refresh_token=""), http_client)

    result = await publisher.publish(MESSAGE, "https://example.com", REQUEST_ID)

    assert result.error.category == "configuration"
    assert "/tumblr/authorize" in result.error.message


# Routes


@pytest.fixture
def client():
    with TestClient(create_app(make_settings(enabled_platforms="")), follow_redirects=False) as test_client:
        yield test_client


def start_authorization(client) -> str:
    response = client.get("/tumblr/authorize")
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def test_authorize_redirects_to_tumblr(client):
    response = client.get("/tumblr/authorize")

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    query = {key: values[0] for key, values in parse_qs(location.query).items()}
    assert location.netloc == "www.tumblr.com"
    assert location.path == "/oauth2/authorize"
    assert query["scope"] == "basic write offline_access"
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == REDIRECT_URI
    assert client.cookies.get("tumblr_oauth_state") == query["state"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "tumblr-secret-client" not in response.headers["location"]


def test_authorize_without_configuration():
    settings = make_settings(tumblr_consumer_key="", tumblr_secret_key="")
    with TestClient(create_app(settings), follow_redirects=False) as client:
        response = client.get("/tumblr/authorize")

    assert response.status_code == 503
    assert "TUMBLR_CONSUMER_KEY" in response.json()["detail"]
    assert "TUMBLR_SECRET_KEY" in response.json()["detail"]


def test_callback_exchanges_code_and_stores_tokens(client, token_route, isolated_token_store, caplog):
    caplog.set_level(logging.DEBUG)
    state = start_authorization(client)

    response = client.get("/tumblr/callback", params={"code": "the-code", "state": state})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "authorized"
    assert body["scope"] == "basic write offline_access"
    assert body["missing_scopes"] == []
    assert body["refresh_token_received"] is True
    assert form(token_route)["code"] == "the-code"

    saved = TokenStore(isolated_token_store).load()
    assert (saved.access_token, saved.refresh_token) == ("new-access", "new-refresh")
    for secret in ("new-access", "new-refresh", "the-code", *SECRETS.values()):
        assert secret not in response.text
        assert secret not in caplog.text
    assert "tumblr_oauth_state" not in client.cookies


def test_callback_reports_missing_write_scope(client, token_route):
    token_route.mock(return_value=httpx.Response(200, json={**token_payload(), "scope": "basic"}))
    state = start_authorization(client)

    body = client.get("/tumblr/callback", params={"code": "c", "state": state}).json()

    assert body["missing_scopes"] == ["write", "offline_access"]


def test_callback_rejects_unknown_state(client, token_route):
    start_authorization(client)

    response = client.get("/tumblr/callback", params={"code": "c", "state": "forged"})

    assert response.status_code == 400
    assert "state" in response.json()["detail"]
    assert not token_route.called


def test_callback_rejects_state_from_another_browser(client, token_route):
    state = start_authorization(client)
    client.cookies.clear()

    response = client.get("/tumblr/callback", params={"code": "c", "state": state})

    assert response.status_code == 400
    assert not token_route.called


def test_callback_rejects_reused_state(client, token_route):
    state = start_authorization(client)
    client.get("/tumblr/callback", params={"code": "c", "state": state})
    client.cookies.set("tumblr_oauth_state", state, path="/tumblr")

    response = client.get("/tumblr/callback", params={"code": "c", "state": state})

    assert response.status_code == 400
    assert token_route.call_count == 1


def test_callback_without_state(client, token_route):
    assert client.get("/tumblr/callback", params={"code": "c"}).status_code == 400
    assert not token_route.called


def test_callback_when_user_denies_access(client, token_route):
    state = start_authorization(client)

    response = client.get(
        "/tumblr/callback",
        params={"state": state, "error": "access_denied", "error_description": "The user denied access"},
    )

    assert response.status_code == 400
    assert "access_denied" in response.json()["detail"]
    assert not token_route.called


def test_callback_without_code(client, token_route):
    state = start_authorization(client)

    assert client.get("/tumblr/callback", params={"state": state}).status_code == 400
    assert not token_route.called


def test_callback_with_rejected_code(client, token_route, isolated_token_store):
    token_route.mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    state = start_authorization(client)

    response = client.get("/tumblr/callback", params={"code": "c", "state": state})

    assert response.status_code == 400
    assert "invalid_grant" in response.json()["detail"]
    assert not isolated_token_store.exists()


def test_callback_when_tumblr_is_unreachable(client, token_route):
    token_route.mock(side_effect=httpx.ConnectError("dns"))
    state = start_authorization(client)

    response = client.get("/tumblr/callback", params={"code": "c", "state": state})

    assert response.status_code == 502
    assert "connect" in response.json()["detail"].lower()


def test_callback_when_tokens_cannot_be_saved(client, token_route, monkeypatch):
    def fail(self, tokens):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(TokenStore, "save", fail)
    state = start_authorization(client)

    response = client.get("/tumblr/callback", params={"code": "c", "state": state})

    assert response.status_code == 500
    assert "could not be saved" in response.json()["detail"]
    assert "new-access" not in response.text


def test_authorized_tokens_are_used_by_the_publisher(client, token_route, mock_api):
    create = mock_api.post(POSTS_URL).mock(return_value=httpx.Response(201, json=CREATED))
    mock_api.get(POSTS_URL).mock(return_value=httpx.Response(200, json=FETCHED))
    state = start_authorization(client)
    client.get("/tumblr/callback", params={"code": "c", "state": state})

    with TestClient(create_app(make_settings(enabled_platforms="tumblr", tumblr_access_token="", tumblr_refresh_token=""))) as api:
        response = api.post("/posting", json={"message": MESSAGE})

    assert response.json()["results"][0]["status"] == "success"
    assert create.calls.last.request.headers["Authorization"] == "Bearer new-access"


async def test_rejected_token_message_names_missing_client_id(http_client, store, token_route):
    manager = manager_for(http_client, tumblr_refresh_token="", tumblr_consumer_key="")

    with pytest.raises(AuthenticationError) as caught:
        await manager.refresh(REQUEST_ID, rejected_access_token="tumblr-secret-access")

    assert "Also missing: TUMBLR_CONSUMER_KEY" in caught.value.message
    assert "tumblr-secret-access" not in caught.value.message


def test_custom_tumblr_urls_are_used(http_client):
    settings = make_settings(tumblr_authorize_url="https://tumblr.test/oauth2/authorize")

    url = TumblrOAuthClient(settings, http_client).authorization_url("s")

    assert url.startswith("https://tumblr.test/oauth2/authorize?")


async def test_refresh_needs_the_token_url(http_client, store, token_route):
    manager = manager_for(http_client, tumblr_token_url="")

    with pytest.raises(AuthenticationError, match="TUMBLR_TOKEN_URL"):
        await manager.refresh(REQUEST_ID, rejected_access_token="tumblr-secret-access")

    assert not token_route.called
