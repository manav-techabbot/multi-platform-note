import json
import time

import httpx
import pytest

from app.publishers.gist import GistPublisher
from app.schemas import DeliveryMode, ResultStatus
from tests.conftest import make_settings
from tests.platforms.shared_cases import (
    CASE_IDS,
    FAILURE_CASES,
    assert_failed,
    check_failure_case,
    publish,
)

GISTS_URL = "https://api.github.com/gists"
GIST = {
    "id": "aa5a315d61ae9438b18d",
    "url": "https://api.github.com/gists/aa5a315d61ae9438b18d",
    "html_url": "https://gist.github.com/aa5a315d61ae9438b18d",
    "public": True,
    "created_at": "2026-11-17T10:30:00Z",
    "files": {"message.md": {"filename": "message.md"}},
    "owner": {"login": "octocat"},
}


@pytest.fixture
def publisher(settings, http_client):
    return GistPublisher(settings, http_client)


@pytest.fixture
def create_route(mock_api):
    return mock_api.post(GISTS_URL)


async def test_creates_public_gist(publisher, create_route):
    create_route.mock(return_value=httpx.Response(201, json=GIST))

    result = await publish(publisher, "Big *news* for <you>: https://example.com/?a=1&b=2")

    assert result.status is ResultStatus.SUCCESS
    assert result.delivery_mode is DeliveryMode.PUBLISHED
    assert result.post_id == GIST["id"]
    assert result.posted_url == GIST["html_url"]
    assert result.public_access is True
    assert (result.date, result.time) == ("2026-11-17", "10:30:00")
    assert result.details is None

    request = create_route.calls.last.request
    assert request.headers["Authorization"] == "Bearer github-secret-token"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert json.loads(request.content) == {
        "description": "Big *news* for <you>",
        "public": True,
        "files": {
            "message.md": {"content": r"Big \*news\* for \<you\>: <https://example.com/?a=1&b=2>"}
        },
    }


async def test_secret_gist_is_not_reported_as_public(http_client, create_route):
    create_route.mock(return_value=httpx.Response(201, json={**GIST, "public": False}))
    publisher = GistPublisher(make_settings(github_gist_public=False), http_client)

    result = await publish(publisher)

    assert json.loads(create_route.calls.last.request.content)["public"] is False
    assert result.status is ResultStatus.SUCCESS
    assert result.public_access is False
    assert result.posted_url == GIST["html_url"]
    assert "Secret gist" in result.details
    assert result.is_real_publication is False


async def test_title_fallback_for_description(publisher, create_route):
    create_route.mock(return_value=httpx.Response(201, json=GIST))

    await publish(publisher, "https://example.com is live")

    assert json.loads(create_route.calls.last.request.content)["description"] == "Quick update"


async def test_missing_html_url_is_not_fabricated(publisher, create_route):
    create_route.mock(return_value=httpx.Response(201, json={**GIST, "html_url": None}))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.posted_url is None
    assert "did not return a public URL" in result.details


async def test_github_error_message_is_surfaced(publisher, create_route):
    create_route.mock(
        return_value=httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"resource": "Gist", "code": "missing_field", "field": "files"}],
                "documentation_url": "https://docs.github.com/rest/gists/gists#create-a-gist",
            },
        )
    )

    result = await publish(publisher)

    assert_failed(result, "content_rejected")
    assert "Validation Failed" in result.error.message


async def test_missing_scope_is_a_permission_error(publisher, create_route):
    create_route.mock(
        return_value=httpx.Response(
            404, json={"message": "Not Found", "documentation_url": "https://docs.github.com"}
        )
    )

    result = await publish(publisher)

    # GitHub answers 404 rather than 403 when a token lacks the gist scope.
    assert_failed(result, "not_found")


async def test_primary_rate_limit_403_waits_for_reset(publisher, create_route, retry_delays):
    reset = str(int(time.time()) + 5)
    create_route.mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset},
                json={"message": "API rate limit exceeded"},
            ),
            httpx.Response(201, json=GIST),
        ]
    )

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert create_route.call_count == 2
    assert 3 <= retry_delays[0] <= 6


async def test_secondary_rate_limit_403_with_retry_after(publisher, create_route, retry_delays):
    create_route.mock(
        return_value=httpx.Response(
            403, headers={"Retry-After": "60"}, json={"message": "You have exceeded a secondary rate limit"}
        )
    )

    result = await publish(publisher)

    # Waiting 60 seconds would exceed the 30-second limit, so it fails without retrying.
    assert_failed(result, "rate_limited")
    assert create_route.call_count == 1
    assert retry_delays == []


async def test_plain_403_is_a_permission_error(publisher, create_route):
    create_route.mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
    )

    result = await publish(publisher)

    assert_failed(result, "permission_denied")
    assert "not accessible" in result.error.message
    assert create_route.call_count == 1


async def test_missing_configuration(empty_settings, http_client, mock_api):
    result = await publish(GistPublisher(empty_settings, http_client))

    assert_failed(result, "configuration")
    for name in ("GITHUB_API_BASE_URL", "GITHUB_API_VERSION", "GITHUB_GIST_TOKEN"):
        assert name in result.error.message
    assert not mock_api.calls


@pytest.mark.parametrize("case", FAILURE_CASES, ids=CASE_IDS)
async def test_failure_handling(publisher, create_route, case):
    await check_failure_case(publisher, create_route, case)
