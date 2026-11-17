import json

import httpx
import pytest

from app.publishers.devto import DevtoPublisher
from app.schemas import DeliveryMode, ResultStatus
from tests.platforms.shared_cases import (
    CASE_IDS,
    FAILURE_CASES,
    assert_failed,
    check_failure_case,
    publish,
)

ARTICLES_URL = "https://dev.to/api/articles"
ARTICLE = {
    "type_of": "article",
    "id": 1947263,
    "title": "Check out our latest business platform",
    "url": "https://dev.to/tester/check-out-our-latest-business-platform-1abc",
    "created_at": "2026-11-17T10:29:58Z",
    "published_at": "2026-11-17T10:30:00Z",
    "published_timestamp": "2026-11-17T10:30:00Z",
}


@pytest.fixture
def publisher(settings, http_client):
    return DevtoPublisher(settings, http_client)


@pytest.fixture
def create_route(mock_api):
    return mock_api.post(ARTICLES_URL)


async def test_publishes_article(publisher, create_route):
    create_route.mock(return_value=httpx.Response(201, json=ARTICLE))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.delivery_mode is DeliveryMode.PUBLISHED
    assert result.post_id == "1947263"
    assert result.posted_url == ARTICLE["url"]
    assert result.public_access is True
    assert (result.date, result.time) == ("2026-11-17", "10:30:00")

    request = create_route.calls.last.request
    assert request.headers["api-key"] == "devto-secret-key"
    assert request.headers["Accept"] == "application/vnd.forem.api-v1+json"
    assert request.headers["User-Agent"] == "automated-posting/1.0"
    assert json.loads(request.content) == {
        "article": {
            "title": "Check out our latest business platform",
            "body_markdown": "Check out our latest business platform. <https://example.com>",
            "published": True,
        }
    }


async def test_title_falls_back_when_message_starts_with_url(publisher, create_route):
    create_route.mock(return_value=httpx.Response(201, json=ARTICLE))

    await publish(publisher, "https://example.com is live", "https://example.com")

    article = json.loads(create_route.calls.last.request.content)["article"]
    assert article["title"] == "Quick update"
    assert "tags" not in article
    assert "description" not in article


async def test_validation_error_from_dev_is_reported(publisher, create_route):
    create_route.mock(
        return_value=httpx.Response(
            422, json={"error": "Title has already been used in the last five minutes", "status": 422}
        )
    )

    result = await publish(publisher)

    assert_failed(result, "content_rejected")
    assert "already been used" in result.error.message


async def test_unpublished_article_is_not_reported_as_public(publisher, create_route):
    unpublished = {**ARTICLE, "published_at": None, "published_timestamp": ""}
    create_route.mock(return_value=httpx.Response(201, json=unpublished))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.public_access is None


async def test_missing_url_is_not_fabricated(publisher, create_route):
    create_route.mock(return_value=httpx.Response(201, json={"id": 5, "published_at": "2026-11-17T10:30:00Z"}))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.posted_url is None
    assert result.public_access is None


async def test_missing_configuration(empty_settings, http_client, mock_api):
    result = await publish(DevtoPublisher(empty_settings, http_client))

    assert_failed(result, "configuration")
    assert "DEVTO_API_KEY" in result.error.message
    assert not mock_api.calls


@pytest.mark.parametrize("case", FAILURE_CASES, ids=CASE_IDS)
async def test_failure_handling(publisher, create_route, case):
    await check_failure_case(publisher, create_route, case)
