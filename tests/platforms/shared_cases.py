"""Failure scenarios that every HTTP-based adapter has to handle the same way."""

from dataclasses import dataclass

import httpx
import respx

from app.publishers.base import Publisher
from app.schemas import PlatformResult, ResultStatus
from app.utils.url_validation import extract_cta_url
from tests.conftest import MESSAGE, REQUEST_ID, SECRETS


@dataclass(frozen=True)
class FailureCase:
    name: str
    reply: httpx.Response | Exception
    category: str
    # How many create requests are expected when a repeat could / could not duplicate a post.
    calls_if_idempotent: int = 1
    calls_otherwise: int = 1


FAILURE_CASES = [
    FailureCase("invalid_parameters", httpx.Response(400, json={"error": "Bad request"}), "invalid_request"),
    FailureCase("authentication_failure", httpx.Response(401, json={"error": "Invalid token"}), "authentication"),
    FailureCase("permission_rejection", httpx.Response(403, json={"error": "Forbidden"}), "permission_denied"),
    FailureCase("unknown_resource", httpx.Response(404, json={"error": "Not found"}), "not_found"),
    FailureCase("conflict", httpx.Response(409, json={"error": "Conflict"}), "conflict"),
    FailureCase("content_rejected", httpx.Response(422, json={"error": "Too long"}), "content_rejected"),
    FailureCase(
        "rate_limited",
        httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "Slow down"}),
        "rate_limited",
        calls_if_idempotent=3,
        calls_otherwise=3,
    ),
    FailureCase(
        "server_error",
        httpx.Response(500, text="Internal Server Error"),
        "platform_unavailable",
        calls_if_idempotent=3,
    ),
    FailureCase(
        "service_unavailable",
        httpx.Response(503, text="Service Unavailable"),
        "platform_unavailable",
        calls_if_idempotent=3,
        calls_otherwise=3,
    ),
    FailureCase(
        "connection_error",
        httpx.ConnectError("DNS lookup failed"),
        "network",
        calls_if_idempotent=3,
        calls_otherwise=3,
    ),
    FailureCase(
        "read_timeout",
        httpx.ReadTimeout("timed out"),
        "uncertain_delivery",
        calls_if_idempotent=3,
    ),
    FailureCase("invalid_json", httpx.Response(200, text="<html>not json</html>"), "unexpected_response"),
    FailureCase("unexpected_structure", httpx.Response(200, json=["unexpected"]), "unexpected_response"),
    FailureCase("missing_post_id", httpx.Response(201, json={"unexpected": True}), "unexpected_response"),
]

CASE_IDS = [case.name for case in FAILURE_CASES]


async def publish(
    publisher: Publisher, message: str = MESSAGE, cta_url: str | None = None
) -> PlatformResult:
    return await publisher.publish(message, cta_url or extract_cta_url(message), REQUEST_ID)


async def check_failure_case(
    publisher: Publisher,
    create_route: respx.Route,
    case: FailureCase,
    *,
    refreshes_token_on_401: bool = False,
) -> None:
    if isinstance(case.reply, Exception):
        create_route.mock(side_effect=case.reply)
    else:
        create_route.mock(return_value=case.reply)

    result = await publish(publisher)

    category = case.category
    if case.name == "read_timeout" and publisher.idempotent:
        category = "network"
    assert_failed(result, category)
    if isinstance(case.reply, httpx.Response) and not case.reply.is_success:
        assert result.error.http_status == case.reply.status_code

    expected_calls = case.calls_if_idempotent if publisher.idempotent else case.calls_otherwise
    if case.name == "authentication_failure" and refreshes_token_on_401:
        expected_calls = 2
    assert create_route.call_count == expected_calls


def assert_failed(result: PlatformResult, category: str) -> None:
    assert result.status is ResultStatus.FAILED
    assert result.error is not None
    assert result.error.category == category
    assert result.posted_url is None
    assert result.post_id is None
    assert result.date is None
    assert result.time is None
    assert result.public_access is None
    for secret in SECRETS.values():
        assert secret not in result.model_dump_json()
