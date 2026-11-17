import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.logging import clean_remote_message, redact
from app.core.retry import RetryPolicy, call_with_retry, parse_retry_after
from app.exceptions import (
    AuthenticationError,
    ConflictError,
    ContentRejectedError,
    InvalidRequestError,
    NetworkError,
    PermissionDeniedError,
    PlatformUnavailableError,
    RateLimitError,
    ResourceNotFoundError,
    UnexpectedResponseError,
    error_from_status,
)
from app.main import create_app
from app.publishers.registry import PUBLISHER_CLASSES, build_publishers
from tests.conftest import MESSAGE, SECRETS, make_settings


@pytest.mark.parametrize(
    "status_code, error_class, retryable",
    [
        (400, InvalidRequestError, False),
        (401, AuthenticationError, False),
        (403, PermissionDeniedError, False),
        (404, ResourceNotFoundError, False),
        (409, ConflictError, False),
        (422, ContentRejectedError, False),
        (429, RateLimitError, True),
        (418, UnexpectedResponseError, False),
        (301, UnexpectedResponseError, False),
        (500, PlatformUnavailableError, False),
        (502, PlatformUnavailableError, False),
        (503, PlatformUnavailableError, True),
        (504, PlatformUnavailableError, False),
    ],
)
def test_status_codes_map_to_errors_for_non_idempotent_requests(status_code, error_class, retryable):
    error = error_from_status(status_code, "message", idempotent=False)

    assert type(error) is error_class
    assert error.retryable is retryable
    assert error.status_code == status_code


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_server_errors_are_retryable_when_duplicates_are_impossible(status_code):
    assert error_from_status(status_code, "message", idempotent=True).retryable is True


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422])
def test_client_errors_are_never_retryable(status_code):
    assert error_from_status(status_code, "message", idempotent=True).retryable is False


class Flaky:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def retry(operation, policy=RetryPolicy()):
    return await call_with_retry(operation, policy, platform="Test", request_id="rid")


async def test_retry_succeeds_after_transient_errors(retry_delays):
    operation = Flaky(NetworkError("down", retryable=True), NetworkError("down", retryable=True), "ok")

    assert await retry(operation) == "ok"
    assert operation.calls == 3
    assert len(retry_delays) == 2


async def test_retry_stops_after_the_configured_limit(retry_delays):
    operation = Flaky(*[NetworkError("down", retryable=True) for _ in range(5)])

    with pytest.raises(NetworkError):
        await retry(operation, RetryPolicy(max_retries=2))

    assert operation.calls == 3
    assert len(retry_delays) == 2


async def test_zero_retries_means_a_single_attempt(retry_delays):
    operation = Flaky(NetworkError("down", retryable=True), "ok")

    with pytest.raises(NetworkError):
        await retry(operation, RetryPolicy(max_retries=0))

    assert operation.calls == 1
    assert retry_delays == []


async def test_non_retryable_error_is_raised_immediately(retry_delays):
    operation = Flaky(PermissionDeniedError("forbidden", status_code=403), "ok")

    with pytest.raises(PermissionDeniedError):
        await retry(operation)

    assert operation.calls == 1
    assert retry_delays == []


async def test_unexpected_exceptions_are_not_swallowed_or_retried():
    operation = Flaky(RuntimeError("bug"), "ok")

    with pytest.raises(RuntimeError):
        await retry(operation)

    assert operation.calls == 1


async def test_backoff_grows_exponentially_with_jitter(retry_delays):
    policy = RetryPolicy(max_retries=4, base_delay=1.0, max_delay=5.0)
    operation = Flaky(*[NetworkError("down", retryable=True) for _ in range(4)], "ok")

    await retry(operation, policy)

    bounds = [(0.5, 1.0), (1.0, 2.0), (2.0, 4.0), (2.5, 5.0)]
    assert len(retry_delays) == 4
    for delay, (low, high) in zip(retry_delays, bounds):
        assert low <= delay <= high


def test_jitter_varies_the_delay():
    policy = RetryPolicy(base_delay=1.0)

    assert len({policy.backoff_delay(2) for _ in range(20)}) > 1


async def test_retry_after_is_respected(retry_delays):
    operation = Flaky(RateLimitError("slow down", status_code=429, retryable=True, retry_after=7), "ok")

    assert await retry(operation) == "ok"
    assert retry_delays == [7]


async def test_retry_after_beyond_the_limit_fails_without_waiting(retry_delays):
    operation = Flaky(RateLimitError("slow down", status_code=429, retryable=True, retry_after=3600), "ok")

    with pytest.raises(RateLimitError):
        await retry(operation, RetryPolicy(max_retry_after=30))

    assert operation.calls == 1
    assert retry_delays == []


def test_retry_after_header_parsing():
    in_ten_seconds = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)

    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("0.5") == 0.5
    assert parse_retry_after("-3") == 0.0
    assert 8 <= parse_retry_after(in_ten_seconds) <= 10
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None


def test_per_platform_retry_configuration():
    settings = make_settings(max_retries=1, platform_max_retries="devto=4, tumblr=0")
    publishers = {publisher.key: publisher for publisher in build_publishers(settings, None)}

    assert publishers["devto"].retry_policy.max_retries == 4
    assert publishers["tumblr"].retry_policy.max_retries == 0
    assert publishers["gist"].retry_policy.max_retries == 1


def test_invalid_retry_override_is_a_configuration_error():
    with pytest.raises(ValueError, match="PLATFORM_MAX_RETRIES"):
        make_settings(platform_max_retries="devto:three")


def test_redaction_and_message_cleanup():
    secrets = ["token-123", "pass word"]

    assert redact("Bearer token-123 with pass word", secrets) == "Bearer *** with ***"
    assert clean_remote_message("line one\n\n   line two", secrets) == "line one line two"
    assert len(clean_remote_message("x" * 1000, secrets)) == 303


def test_settings_list_every_secret():
    assert sorted(make_settings().secret_values()) == sorted(SECRETS.values())
    assert Settings(_env_file=None).secret_values() == []


def test_registry_contains_exactly_the_five_platforms():
    assert list(PUBLISHER_CLASSES) == [
        "devto",
        "bluesky",
        "tumblr",
        "gist",
        "jsonplaceholder",
    ]


def test_enabled_platforms_control_which_publishers_are_built():
    settings = make_settings(enabled_platforms=" DevTo, jsonplaceholder ,devto,")

    assert [publisher.key for publisher in build_publishers(settings, None)] == ["devto", "jsonplaceholder"]


def test_unknown_platform_is_reported_at_startup():
    with pytest.raises(ValueError, match="myspace"):
        build_publishers(make_settings(enabled_platforms="devto,myspace"), None)


def post_message(settings: Settings) -> httpx.Response:
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        return client.post("/posting", json={"message": MESSAGE})


def test_missing_credentials_fail_per_platform_without_crashing(mock_api):
    response = post_message(Settings(_env_file=None))

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["partial_success"] is False
    assert body["simulated_count"] == 0
    assert len(body["results"]) == 5
    for result in body["results"]:
        assert result["status"] == "failed"
        assert result["error"]["category"] == "configuration"
        assert "Missing settings" in result["error"]["message"]
    assert "JSONPLACEHOLDER_BASE_URL" in body["results"][4]["error"]["message"]
    assert not mock_api.calls


@pytest.mark.parametrize(
    "field",
    ["devto_base_url", "github_api_base_url", "bluesky_service_url", "tumblr_token_url", "tumblr_redirect_uri"],
)
def test_url_settings_must_be_absolute(field):
    with pytest.raises(ValueError, match=f"{field.upper()} must start with http"):
        make_settings(**{field: "example.com/api"})


@pytest.mark.parametrize(
    "publisher_key, url_setting",
    [
        ("devto", "DEVTO_BASE_URL"),
        ("bluesky", "BLUESKY_SERVICE_URL"),
        ("bluesky", "BLUESKY_WEB_URL"),
        ("tumblr", "TUMBLR_API_BASE_URL"),
        ("gist", "GITHUB_API_BASE_URL"),
        ("jsonplaceholder", "JSONPLACEHOLDER_BASE_URL"),
    ],
)
def test_missing_api_url_is_reported_per_platform(publisher_key, url_setting):
    settings = make_settings(**{url_setting.lower(): ""})
    publisher = next(p for p in build_publishers(settings, None) if p.key == publisher_key)

    assert url_setting in publisher.missing_settings()


def test_every_platform_failing_still_returns_consolidated_results(mock_api, caplog):
    caplog.set_level(logging.DEBUG)
    mock_api.route().mock(return_value=httpx.Response(500, text="upstream exploded"))

    response = post_message(make_settings())

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert [result["status"] for result in body["results"]] == ["failed"] * 5
    for result in body["results"]:
        assert result["posted_url"] is None
        assert result["date"] is None and result["time"] is None
        assert result["error"]["message"]
    for secret in SECRETS.values():
        assert secret not in response.text
        assert secret not in caplog.text
    assert "Traceback" not in response.text


def test_network_outage_on_every_platform(mock_api):
    mock_api.route().mock(side_effect=httpx.ConnectError("Name or service not known"))

    response = post_message(make_settings())

    assert response.status_code == 200
    categories = {result["platform"]: result["error"]["category"] for result in response.json()["results"]}
    assert set(categories.values()) == {"network"}
    assert len(categories) == 5


def test_unhandled_server_error_is_sanitized(monkeypatch):
    async def explode(self, message, cta_url):
        raise RuntimeError("secret internals devto-secret-key")

    monkeypatch.setattr("app.services.posting_service.PostingService.publish", explode)

    response = post_message(make_settings(enabled_platforms=""))

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "devto-secret-key" not in response.text
