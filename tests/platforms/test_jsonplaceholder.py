import json

import httpx
import pytest

from app.config import Settings
from app.publishers.jsonplaceholder import JsonPlaceholderPublisher
from app.schemas import DeliveryMode, ResultStatus
from tests.conftest import MESSAGE, make_settings
from tests.platforms.shared_cases import (
    CASE_IDS,
    FAILURE_CASES,
    assert_failed,
    check_failure_case,
    publish,
)

POSTS_URL = "https://jsonplaceholder.typicode.com/posts"
SENT = {"title": "Check out our latest business platform", "body": MESSAGE, "userId": 1}


@pytest.fixture
def publisher(settings, http_client):
    return JsonPlaceholderPublisher(settings, http_client)


@pytest.fixture
def create_route(mock_api):
    return mock_api.post(POSTS_URL)


async def test_simulated_post_is_reported_as_simulated(publisher, create_route, mock_api):
    create_route.mock(return_value=httpx.Response(201, json={**SENT, "id": 101}))

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert result.delivery_mode is DeliveryMode.SIMULATED
    assert result.posted_url is None
    assert result.post_id is None
    assert result.public_access is False
    assert result.date is None
    assert result.time is None
    assert result.error is None
    assert "101" in result.details
    assert "not persisted" in result.details
    assert result.is_real_publication is False

    request = create_route.calls.last.request
    assert request.headers["Content-Type"] == "application/json; charset=UTF-8"
    assert "Authorization" not in request.headers
    assert json.loads(request.content) == SENT
    # No follow-up GET is made to pretend the resource exists.
    assert len(mock_api.calls) == 1


async def test_needs_only_the_base_url(http_client, create_route):
    create_route.mock(return_value=httpx.Response(201, json={**SENT, "id": 101}))
    settings = Settings(_env_file=None, jsonplaceholder_base_url="https://jsonplaceholder.typicode.com")

    result = await publish(JsonPlaceholderPublisher(settings, http_client))

    assert result.status is ResultStatus.SUCCESS


async def test_user_id_is_configurable(http_client, create_route):
    create_route.mock(return_value=httpx.Response(201, json={**SENT, "userId": 7, "id": 101}))
    publisher = JsonPlaceholderPublisher(make_settings(jsonplaceholder_user_id=7), http_client)

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert json.loads(create_route.calls.last.request.content)["userId"] == 7


async def test_response_that_does_not_echo_the_payload_is_rejected(publisher, create_route):
    create_route.mock(return_value=httpx.Response(201, json={**SENT, "body": "something else", "id": 101}))

    result = await publish(publisher)

    assert_failed(result, "unexpected_response")
    assert "body" in result.error.message


async def test_transient_failure_is_retried(publisher, create_route, retry_delays):
    create_route.mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(201, json={**SENT, "id": 101})]
    )

    result = await publish(publisher)

    assert result.status is ResultStatus.SUCCESS
    assert create_route.call_count == 2
    assert len(retry_delays) == 1


async def test_missing_configuration(http_client, mock_api):
    publisher = JsonPlaceholderPublisher(make_settings(jsonplaceholder_base_url=""), http_client)

    result = await publish(publisher)

    assert_failed(result, "configuration")
    assert not mock_api.calls


@pytest.mark.parametrize("case", FAILURE_CASES, ids=CASE_IDS)
async def test_failure_handling(publisher, create_route, case):
    await check_failure_case(publisher, create_route, case)
