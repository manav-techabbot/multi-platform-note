import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.publishers.tumblr import TumblrPublisher
from app.schemas import DeliveryMode, ResultStatus
from tests.conftest import MESSAGE, make_settings
from tests.platforms.shared_cases import (
    CASE_IDS,
    FAILURE_CASES,
    assert_failed,
    check_failure_case,
    publish,
)

POSTS_URL = "https://api.tumblr.com/v2/blog/testblog/posts"
TOKEN_URL = "https://api.tumblr.com/v2/oauth2/token"
POST_ID = "756123456789012345"
CREATED = {"meta": {"status": 201, "msg": "Created"}, "response": {"id": POST_ID}}
FETCHED = {
    "meta": {"status": 200, "msg": "OK"},
    "response": {
        "posts": [
            {
                "id_string": POST_ID,
                "post_url": f"https://testblog.tumblr.com/post/{POST_ID}/check-out-our-latest",
                "state": "published",
                "timestamp": 1794911400,
                "date": "2026-11-17 10:30:00 GMT",
            }
        ]
    },
}


@pytest.fixture
def publisher(settings, http_client):
    return TumblrPublisher(settings, http_client)


@pytest.fixture
def create_route(mock_api):
    return mock_api.post(POSTS_URL)


@pytest.fixture
def fetch_route(mock_api):
    return mock_api.get(POSTS_URL).mock(return_value=httpx.Response(200, json=FETCHED))


@pytest.fixture
def token_route(mock_api):
    return mock_api.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "tumblr-access-2",
                "refresh_token": "tumblr-refresh-2",
                "expires_in": 2520,
                "token_type": "bearer",
                "scope": "write offline_access",
            },
        )
    )


async def test_publishes_npf_post_and_reads_back_url(publisher, create_route, fetch_route):
    create_route.mock(return_value=httpx.Response(201, json=CREATED))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.delivery_mode is DeliveryMode.PUBLISHED
    assert result.post_id == POST_ID
    assert result.posted_url == FETCHED["response"]["posts"][0]["post_url"]
    assert result.public_access is True
    assert (result.date, result.time) == ("2026-11-17", "10:30:00")

    request = create_route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tumblr-secret-access"
    assert request.headers["User-Agent"] == "automated-posting/1.0"
    assert json.loads(request.content) == {
        "content": [
            {
                "type": "text",
                "text": MESSAGE,
                "formatting": [
                    {"start": 40, "end": 59, "type": "link", "url": "https://example.com"}
                ],
            }
        ],
        "state": "published",
    }
    assert MESSAGE[40:59] == "https://example.com"

    lookup = fetch_route.calls.last.request
    assert lookup.url.params["id"] == POST_ID
    assert lookup.url.params["npf"] == "true"


async def test_url_is_not_fabricated_when_post_cannot_be_read_back(publisher, create_route, fetch_route):
    create_route.mock(return_value=httpx.Response(201, json=CREATED))
    fetch_route.mock(return_value=httpx.Response(404, json={"meta": {"status": 404, "msg": "Not Found"}}))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.post_id == POST_ID
    assert result.posted_url is None
    assert result.public_access is None
    assert "could not be read back" in result.details
    assert create_route.call_count == 1


async def test_non_published_state_is_not_public(publisher, create_route, fetch_route):
    create_route.mock(return_value=httpx.Response(201, json=CREATED))
    private_post = {**FETCHED["response"]["posts"][0], "state": "private"}
    fetch_route.mock(return_value=httpx.Response(200, json={"response": {"posts": [private_post]}}))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.public_access is False
    assert result.posted_url is None


async def test_expired_access_token_is_refreshed_once(publisher, create_route, fetch_route, token_route):
    create_route.mock(
        side_effect=[
            httpx.Response(401, json={"meta": {"status": 401, "msg": "Unauthorized"}}),
            httpx.Response(201, json=CREATED),
        ]
    )

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert token_route.call_count == 1
    assert parse_qs(token_route.calls.last.request.content.decode()) == {
        "grant_type": ["refresh_token"],
        "client_id": ["tumblr-client-id"],
        "client_secret": ["tumblr-secret-client"],
        "refresh_token": ["tumblr-secret-refresh"],
    }
    assert create_route.calls.last.request.headers["Authorization"] == "Bearer tumblr-access-2"
    assert fetch_route.calls.last.request.headers["Authorization"] == "Bearer tumblr-access-2"


async def test_rejected_token_without_refresh_token(http_client, create_route, token_route):
    publisher = TumblrPublisher(make_settings(tumblr_refresh_token=""), http_client)
    create_route.mock(return_value=httpx.Response(401, json={"meta": {"status": 401, "msg": "Unauthorized"}}))

    result = await publish(publisher)

    assert_failed(result, "authentication")
    assert not token_route.called
    assert create_route.call_count == 1


async def test_token_refresh_failure(publisher, create_route, token_route):
    create_route.mock(return_value=httpx.Response(401, json={"meta": {"status": 401, "msg": "Unauthorized"}}))
    token_route.mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid refresh token"})
    )

    result = await publish(publisher)

    assert_failed(result, "authentication")
    assert "rejected the refresh token" in result.error.message
    assert "/tumblr/authorize" in result.error.message


async def test_tumblr_error_detail_is_surfaced(publisher, create_route):
    create_route.mock(
        return_value=httpx.Response(
            400,
            json={
                "meta": {"status": 400, "msg": "Bad Request"},
                "errors": [{"title": "Bad Request", "code": 8001, "detail": "'content' must be an array"}],
            },
        )
    )

    result = await publish(publisher)

    assert_failed(result, "invalid_request")
    assert "'content' must be an array" in result.error.message


async def test_missing_configuration(empty_settings, http_client, mock_api):
    result = await publish(TumblrPublisher(empty_settings, http_client))

    assert_failed(result, "configuration")
    assert "TUMBLR_BLOG_IDENTIFIER" in result.error.message
    assert "TUMBLR_ACCESS_TOKEN" in result.error.message
    assert not mock_api.calls


async def test_refresh_credentials_alone_are_enough(http_client, create_route, fetch_route, token_route):
    publisher = TumblrPublisher(make_settings(tumblr_access_token=""), http_client)
    create_route.mock(return_value=httpx.Response(201, json=CREATED))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert token_route.call_count == 1
    assert create_route.calls.last.request.headers["Authorization"] == "Bearer tumblr-access-2"


@pytest.mark.parametrize("case", FAILURE_CASES, ids=CASE_IDS)
async def test_failure_handling(publisher, create_route, token_route, case):
    await check_failure_case(publisher, create_route, case, refreshes_token_on_401=True)
