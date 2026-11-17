import asyncio
import logging

import httpx
import pytest

from app.exceptions import AuthenticationError, PublishError
from app.publishers.base import Publisher
from app.publishers.devto import DevtoPublisher
from app.publishers.gist import GistPublisher
from app.publishers.jsonplaceholder import JsonPlaceholderPublisher
from app.schemas import DeliveryMode, PlatformResult, ResultStatus
from app.services.posting_service import PostingService
from app.utils.datetime_utils import parse_timestamp
from tests.conftest import CTA_URL, MESSAGE, SECRETS, make_settings

DEVTO_URL = "https://dev.to/api/articles"
GIST_URL = "https://api.github.com/gists"
JSONPLACEHOLDER_URL = "https://jsonplaceholder.typicode.com/posts"

DEVTO_ARTICLE = {
    "id": 1,
    "url": "https://dev.to/tester/quick-update-1abc",
    "published_at": "2026-11-17T10:30:00Z",
}
GIST = {
    "id": "aa5a315d61ae9438b18d",
    "html_url": "https://gist.github.com/aa5a315d61ae9438b18d",
    "created_at": "2026-11-17T10:30:00Z",
    "public": True,
}


class StubPublisher(Publisher):
    """Publisher whose outcome is scripted, for testing the service in isolation."""

    key = "stub"

    def __init__(self, name: str, outcome: str = "success", delay_ticks: int = 0) -> None:
        super().__init__(make_settings(), http_client=None)
        self.name = name
        self.outcome = outcome
        self.delay_ticks = delay_ticks
        self.finished = False

    def missing_settings(self) -> list[str]:
        return []

    async def publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        if self.outcome == "crash":
            raise RuntimeError("adapter bug with secret github-secret-token")
        return await super().publish(message, cta_url, request_id)

    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        for _ in range(self.delay_ticks):
            await asyncio.sleep(0)
        self.finished = True
        if self.outcome == "fail":
            raise AuthenticationError(f"{self.name} returned HTTP 401", status_code=401)
        return self.success(
            post_id="1",
            posted_url=f"https://{self.name.lower()}.example/post/1",
            public_access=True,
            published_at=parse_timestamp("2026-11-17T10:30:00Z"),
        )


class SimulatedStubPublisher(StubPublisher):
    delivery_mode = DeliveryMode.SIMULATED

    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        return PlatformResult(
            platform=self.name,
            status=ResultStatus.SUCCESS,
            delivery_mode=self.delivery_mode,
            public_access=False,
            details="Simulated POST accepted.",
        )


async def run(*publishers: Publisher):
    return await PostingService(list(publishers)).publish(MESSAGE, CTA_URL)


def statuses(response) -> dict[str, str]:
    return {result.platform: result.status.value for result in response.results}


async def test_all_real_publishers_succeed():
    response = await run(StubPublisher("Alpha"), StubPublisher("Beta"), StubPublisher("Gamma"))

    assert response.success is True
    assert response.partial_success is False
    assert response.simulated_count == 0
    assert statuses(response) == {"Alpha": "success", "Beta": "success", "Gamma": "success"}


async def test_one_publisher_fails_and_the_others_succeed():
    response = await run(StubPublisher("Alpha"), StubPublisher("Beta", "fail"), StubPublisher("Gamma"))

    assert response.success is True
    assert response.partial_success is True
    assert statuses(response) == {"Alpha": "success", "Beta": "failed", "Gamma": "success"}
    failed = response.results[1]
    assert failed.error.category == "authentication"
    assert failed.error.http_status == 401
    assert failed.posted_url is None and failed.date is None and failed.time is None


async def test_multiple_publishers_fail():
    response = await run(StubPublisher("Alpha", "fail"), StubPublisher("Beta", "fail"), StubPublisher("Gamma"))

    assert response.success is True
    assert response.partial_success is True
    assert statuses(response) == {"Alpha": "failed", "Beta": "failed", "Gamma": "success"}


async def test_all_real_publishers_fail():
    response = await run(StubPublisher("Alpha", "fail"), StubPublisher("Beta", "fail"))

    assert response.success is False
    assert response.partial_success is False
    assert response.simulated_count == 0
    assert len(response.results) == 2


async def test_simulated_success_does_not_count_as_real_publication():
    response = await run(
        StubPublisher("Alpha", "fail"),
        StubPublisher("Beta", "fail"),
        SimulatedStubPublisher("JSONPlaceholder"),
    )

    assert response.success is False
    assert response.partial_success is False
    assert response.simulated_count == 1
    simulated = response.results[2]
    assert simulated.status is ResultStatus.SUCCESS
    assert simulated.delivery_mode is DeliveryMode.SIMULATED


async def test_only_a_simulated_publisher_is_not_a_success():
    response = await run(SimulatedStubPublisher("JSONPlaceholder"))

    assert response.success is False
    assert response.simulated_count == 1


async def test_real_success_next_to_simulated_result_is_partial():
    response = await run(StubPublisher("Alpha"), SimulatedStubPublisher("JSONPlaceholder"))

    assert response.success is True
    assert response.partial_success is True
    assert response.simulated_count == 1


async def test_no_enabled_publishers():
    response = await run()

    assert response.success is False
    assert response.results == []


async def test_unexpected_exception_is_contained(caplog):
    caplog.set_level(logging.INFO)

    response = await run(StubPublisher("Alpha"), StubPublisher("Beta", "crash"), StubPublisher("Gamma"))

    assert statuses(response) == {"Alpha": "success", "Beta": "failed", "Gamma": "success"}
    crashed = response.results[1]
    assert crashed.error.category == "internal_error"
    assert "RuntimeError" not in crashed.error.message
    assert "Traceback" not in response.model_dump_json()
    assert "github-secret-token" not in response.model_dump_json()


async def test_failure_does_not_cancel_slower_publishers():
    slow = StubPublisher("Slow", delay_ticks=20)
    slower = StubPublisher("Slower", delay_ticks=40)

    response = await run(StubPublisher("Crash", "crash"), StubPublisher("Fail", "fail"), slow, slower)

    assert slow.finished and slower.finished
    assert statuses(response) == {
        "Crash": "failed",
        "Fail": "failed",
        "Slow": "success",
        "Slower": "success",
    }


async def test_publishers_run_concurrently():
    started: list[str] = []
    release = asyncio.Event()

    class WaitingPublisher(StubPublisher):
        async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
            started.append(self.name)
            if len(started) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=1)
            return await super()._publish(message, cta_url, request_id)

    response = await run(WaitingPublisher("Alpha"), WaitingPublisher("Beta"))

    assert response.success is True
    assert sorted(started) == ["Alpha", "Beta"]


async def test_results_keep_registry_order():
    response = await run(
        StubPublisher("Slow", delay_ticks=10), StubPublisher("Fast"), StubPublisher("Failing", "fail")
    )

    assert [result.platform for result in response.results] == ["Slow", "Fast", "Failing"]


async def test_every_publisher_gets_the_same_request_id():
    seen: list[str] = []

    class RecordingPublisher(StubPublisher):
        async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
            seen.append(request_id)
            return await super()._publish(message, cta_url, request_id)

    await run(RecordingPublisher("Alpha"), RecordingPublisher("Beta"))

    assert len(seen) == 2 and len(set(seen)) == 1


@pytest.fixture
def real_publishers(settings, http_client):
    return (
        GistPublisher(settings, http_client),
        DevtoPublisher(settings, http_client),
        JsonPlaceholderPublisher(settings, http_client),
    )


async def test_retryable_errors_follow_the_retry_policy(real_publishers, mock_api, retry_delays):
    gist = mock_api.post(GIST_URL).mock(side_effect=[httpx.Response(503), httpx.Response(201, json=GIST)])
    devto = mock_api.post(DEVTO_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.ConnectError("connection refused"),
            httpx.Response(201, json=DEVTO_ARTICLE),
        ]
    )
    # JSONPlaceholder stores nothing, so even a 500 is safe to repeat.
    placeholder = mock_api.post(JSONPLACEHOLDER_URL).mock(return_value=httpx.Response(500))

    response = await run(*real_publishers)

    assert statuses(response) == {
        "GitHub Gist": "success",
        "DEV Community": "success",
        "JSONPlaceholder": "failed",
    }
    assert gist.call_count == 2
    assert devto.call_count == 3
    assert placeholder.call_count == 3
    assert 3.0 in retry_delays
    assert response.success is True
    assert response.simulated_count == 0


async def test_server_error_on_a_real_platform_is_not_reposted(real_publishers, mock_api):
    gist = mock_api.post(GIST_URL).mock(return_value=httpx.Response(500))
    mock_api.post(DEVTO_URL).mock(return_value=httpx.Response(201, json=DEVTO_ARTICLE))
    mock_api.post(JSONPLACEHOLDER_URL).mock(return_value=httpx.Response(400))

    response = await run(*real_publishers)

    assert gist.call_count == 1
    assert response.results[0].error.category == "platform_unavailable"


async def test_permanent_errors_are_not_retried(real_publishers, mock_api, retry_delays):
    gist = mock_api.post(GIST_URL).mock(return_value=httpx.Response(422, json={"message": "Validation Failed"}))
    devto = mock_api.post(DEVTO_URL).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))
    placeholder = mock_api.post(JSONPLACEHOLDER_URL).mock(return_value=httpx.Response(400))

    response = await run(*real_publishers)

    assert response.success is False
    assert [route.call_count for route in (gist, devto, placeholder)] == [1, 1, 1]
    assert retry_delays == []
    assert [result.error.category for result in response.results] == [
        "content_rejected",
        "permission_denied",
        "invalid_request",
    ]


async def test_ambiguous_timeout_is_not_reposted(real_publishers, mock_api):
    mock_api.post(GIST_URL).mock(return_value=httpx.Response(201, json=GIST))
    devto = mock_api.post(DEVTO_URL).mock(side_effect=httpx.ReadTimeout("no response"))
    mock_api.post(JSONPLACEHOLDER_URL).mock(return_value=httpx.Response(400))

    response = await run(*real_publishers)

    assert devto.call_count == 1
    uncertain = response.results[1]
    assert uncertain.status is ResultStatus.FAILED
    assert uncertain.error.category == "uncertain_delivery"
    assert "may or may not have been created" in uncertain.error.message
    assert response.success is True
    assert response.partial_success is True


async def test_dates_and_times_are_normalized_to_utc(real_publishers, mock_api):
    mock_api.post(GIST_URL).mock(
        return_value=httpx.Response(201, json={**GIST, "created_at": "2026-11-17T23:45:10Z"})
    )
    mock_api.post(DEVTO_URL).mock(
        return_value=httpx.Response(201, json={**DEVTO_ARTICLE, "published_at": "2026-11-18T01:15:00+05:30"})
    )
    mock_api.post(JSONPLACEHOLDER_URL).mock(return_value=httpx.Response(400))

    response = await run(*real_publishers)

    gist, devto, placeholder = response.results
    assert (gist.date, gist.time) == ("2026-11-17", "23:45:10")
    assert (devto.date, devto.time) == ("2026-11-17", "19:45:00")
    assert (placeholder.date, placeholder.time) == (None, None)


async def test_credentials_never_reach_results_or_logs(real_publishers, mock_api, caplog):
    caplog.set_level(logging.DEBUG)
    mock_api.post(GIST_URL).mock(
        return_value=httpx.Response(401, json={"message": "Token github-secret-token is invalid"})
    )
    mock_api.post(DEVTO_URL).mock(
        return_value=httpx.Response(401, json={"error": "unauthorized key devto-secret-key"})
    )
    mock_api.post(JSONPLACEHOLDER_URL).mock(side_effect=httpx.ConnectError("refused"))

    response = await run(*real_publishers)

    assert response.success is False
    exposed = response.model_dump_json() + caplog.text
    for secret in SECRETS.values():
        assert secret not in exposed
    assert "***" in response.results[0].error.message


def test_publish_error_defaults():
    error = PublishError("something failed")

    assert error.retryable is False
    assert error.status_code is None
    assert error.retry_after is None
