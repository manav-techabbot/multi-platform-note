"""Bluesky adapter tests.

These run the real atproto SDK and mock the XRPC endpoints underneath it, so the
record that would be sent to Bluesky is checked exactly as the SDK serializes it.
"""

import base64
import json
import time

import httpx
import pytest

from app.publishers.bluesky import BlueskyPublisher
from app.schemas import DeliveryMode, ResultStatus
from tests.conftest import MESSAGE, make_settings
from tests.platforms.shared_cases import (
    CASE_IDS,
    FAILURE_CASES,
    assert_failed,
    check_failure_case,
    publish,
)

XRPC_URL = "https://bsky.social/xrpc"
DID = "did:plc:abc123"
POST_URI = f"at://{DID}/app.bsky.feed.post/3lxyzrkey"
POST_CID = "bafyreib2rxk3rh6kzwq"
CREATED = {"uri": POST_URI, "cid": POST_CID}


def fake_jwt(scope: str) -> str:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    now = int(time.time())
    claims = {"scope": scope, "sub": DID, "iat": now, "exp": now + 7200, "aud": "did:web:bsky.social"}
    return f"{encode({'alg': 'ES256K', 'typ': 'at+jwt'})}.{encode(claims)}.c2lnbmF0dXJl"


def session_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "accessJwt": fake_jwt("com.atproto.appPass"),
            "refreshJwt": fake_jwt("com.atproto.refresh"),
            "handle": "tester.bsky.social",
            "did": DID,
        },
    )


@pytest.fixture
async def publisher(settings):
    publisher = BlueskyPublisher(settings, http_client=None)
    yield publisher
    await publisher.aclose()


@pytest.fixture
def login_route(mock_api):
    mock_api.get(f"{XRPC_URL}/app.bsky.actor.getProfile").mock(
        return_value=httpx.Response(200, json={"did": DID, "handle": "tester.bsky.social"})
    )
    return mock_api.post(f"{XRPC_URL}/com.atproto.server.createSession").mock(
        return_value=session_response()
    )


@pytest.fixture
def create_route(mock_api, login_route):
    return mock_api.post(f"{XRPC_URL}/com.atproto.repo.createRecord")


async def test_publishes_post_with_link_facet(publisher, create_route, login_route):
    create_route.mock(return_value=httpx.Response(200, json=CREATED))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.delivery_mode is DeliveryMode.PUBLISHED
    assert result.post_id == POST_URI
    assert result.posted_url == "https://bsky.app/profile/tester.bsky.social/post/3lxyzrkey"
    assert result.public_access is True
    assert result.date is not None and result.time is not None
    assert POST_URI in result.details and POST_CID in result.details

    login = json.loads(login_route.calls.last.request.content)
    assert login == {"identifier": "tester.bsky.social", "password": "bluesky-secret-password"}

    sent = json.loads(create_route.calls.last.request.content)
    assert sent["repo"] == DID
    assert sent["collection"] == "app.bsky.feed.post"
    record = sent["record"]
    assert record["text"] == MESSAGE
    assert len(record["facets"]) == 1
    facet = record["facets"][0]
    assert facet["features"] == [
        {"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com"}
    ]
    start, end = facet["index"]["byteStart"], facet["index"]["byteEnd"]
    assert MESSAGE.encode()[start:end] == b"https://example.com"


async def test_facet_offsets_are_utf8_bytes(publisher, create_route):
    create_route.mock(return_value=httpx.Response(200, json=CREATED))
    message = "Café ☕ für alle 👉 https://example.com/menü jetzt!"

    result = await publish(publisher, message, "https://example.com/menü")

    assert result.status is ResultStatus.SUCCESS
    record = json.loads(create_route.calls.last.request.content)["record"]
    index = record["facets"][0]["index"]
    assert record["text"] == message
    assert message.encode()[index["byteStart"]:index["byteEnd"]].decode() == "https://example.com/menü"


async def test_session_is_reused_between_posts(publisher, create_route, login_route):
    create_route.mock(return_value=httpx.Response(200, json=CREATED))

    await publish(publisher)
    await publish(publisher)

    assert create_route.call_count == 2
    assert login_route.call_count == 1


async def test_invalid_app_password(publisher, create_route, login_route):
    login_route.mock(
        return_value=httpx.Response(
            401, json={"error": "AuthenticationRequired", "message": "Invalid identifier or password"}
        )
    )

    result = await publish(publisher)

    assert_failed(result, "authentication")
    assert "Invalid identifier or password" in result.error.message
    assert not create_route.called


async def test_rejected_session_logs_in_again_once(publisher, create_route, login_route):
    create_route.mock(
        side_effect=[
            httpx.Response(401, json={"error": "InvalidToken", "message": "Token could not be verified"}),
            httpx.Response(200, json=CREATED),
        ]
    )

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert login_route.call_count == 2
    assert create_route.call_count == 2


async def test_login_connection_error_is_retried(publisher, create_route, login_route):
    login_route.mock(side_effect=[httpx.ConnectError("refused"), session_response()])
    create_route.mock(return_value=httpx.Response(200, json=CREATED))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert login_route.call_count == 2


async def test_url_is_not_built_from_an_unexpected_record_uri(publisher, create_route):
    create_route.mock(
        return_value=httpx.Response(200, json={"uri": f"at://{DID}/app.bsky.feed.like/3lxyz", "cid": POST_CID})
    )

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.posted_url is None
    assert result.public_access is None


async def test_email_login_uses_did_in_public_url(create_route):
    publisher = BlueskyPublisher(make_settings(bluesky_handle="tester@example.com"), http_client=None)
    create_route.mock(return_value=httpx.Response(200, json=CREATED))

    result = await publish(publisher)
    await publisher.aclose()

    assert result.posted_url == f"https://bsky.app/profile/{DID}/post/3lxyzrkey"


async def test_missing_configuration(empty_settings, mock_api):
    result = await publish(BlueskyPublisher(empty_settings, http_client=None))

    assert_failed(result, "configuration")
    assert "BLUESKY_HANDLE" in result.error.message
    assert "BLUESKY_APP_PASSWORD" in result.error.message
    assert not mock_api.calls


@pytest.mark.parametrize("case", FAILURE_CASES, ids=CASE_IDS)
async def test_failure_handling(publisher, create_route, case):
    await check_failure_case(publisher, create_route, case, refreshes_token_on_401=True)
