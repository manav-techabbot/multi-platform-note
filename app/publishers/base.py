import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar

import httpx

from app.config import Settings
from app.core.logging import clean_remote_message
from app.core.retry import RetryPolicy, call_with_retry, parse_retry_after
from app.exceptions import (
    ConfigurationError,
    NetworkError,
    PublishError,
    UncertainDeliveryError,
    UnexpectedResponseError,
    error_from_status,
)
from app.schemas import DeliveryMode, PlatformError, PlatformResult, ResultStatus
from app.utils.datetime_utils import split_date_time, utc_now

logger = logging.getLogger(__name__)

TIMESTAMP_FALLBACK_NOTE = (
    "The platform returned no publication timestamp; date and time are when the API "
    "confirmed the post (UTC)."
)


class Publisher(ABC):
    key: ClassVar[str]
    name: ClassVar[str]
    delivery_mode: ClassVar[DeliveryMode] = DeliveryMode.PUBLISHED
    # True only when repeating the create request cannot produce a duplicate post.
    idempotent: ClassVar[bool] = False

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http_client
        self.retry_policy = RetryPolicy(max_retries=settings.max_retries_for(self.key))
        self._configured_secrets = settings.secret_values()

    @abstractmethod
    def missing_settings(self) -> list[str]:
        """Names of the environment variables that still need a value."""

    @abstractmethod
    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        """Publish the message or raise a PublishError."""

    def secrets(self) -> list[str]:
        """Values that must be masked in any text taken from a platform response."""
        return self._configured_secrets

    def check_configuration(self) -> None:
        missing = self.missing_settings()
        if missing:
            raise ConfigurationError(
                f"{self.name} is not configured. Missing settings: {', '.join(missing)}"
            )

    async def publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        try:
            self.check_configuration()
            result = await self._publish(message, cta_url, request_id)
        except PublishError as error:
            logger.warning(
                "[%s] %s failed (%s): %s", request_id, self.name, error.category, error.message
            )
            return failed_result(self, error.category, error.message, error.status_code)
        logger.info("[%s] %s %s", request_id, self.name, result.delivery_mode.value)
        return result

    async def aclose(self) -> None:
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        request_id: str,
        idempotent: bool | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request through the shared client and return only successful responses."""
        safe_to_repeat = self.idempotent if idempotent is None else idempotent
        return await call_with_retry(
            lambda: self._request_once(method, url, safe_to_repeat, **kwargs),
            self.retry_policy,
            platform=self.name,
            request_id=request_id,
        )

    async def _request_once(
        self, method: str, url: str, idempotent: bool, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = await self.http.request(method, url, **kwargs)
        except (httpx.InvalidURL, httpx.UnsupportedProtocol):
            raise ConfigurationError(f"{self.name} is configured with an invalid URL") from None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # Nothing was transmitted, so trying again cannot create a duplicate.
            raise NetworkError(f"Could not connect to {self.name}", retryable=True) from None
        except httpx.TimeoutException:
            raise self._interrupted_error("timed out", idempotent) from None
        except httpx.TransportError:
            raise self._interrupted_error("was interrupted", idempotent) from None

        if response.is_success:
            return response
        raise self.classify_error_response(response, idempotent)

    def classify_error_response(self, response: httpx.Response, idempotent: bool) -> PublishError:
        """Turn an unsuccessful response into an error. Override for platform quirks."""
        return error_from_status(
            response.status_code,
            self._describe_error_response(response),
            idempotent=idempotent,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )

    def _interrupted_error(self, what_happened: str, idempotent: bool) -> PublishError:
        if idempotent:
            return NetworkError(f"The request to {self.name} {what_happened}", retryable=True)
        return UncertainDeliveryError(
            f"The request to {self.name} {what_happened} after it was sent. The post may or "
            "may not have been created, so it was not retried. Check the account before "
            "posting again."
        )

    def _describe_error_response(self, response: httpx.Response) -> str:
        description = f"{self.name} returned HTTP {response.status_code}"
        try:
            remote_message = self.extract_error_message(response.json())
        except ValueError:
            remote_message = None
        if remote_message:
            description += f": {clean_remote_message(remote_message, self.secrets())}"
        return description

    def extract_error_message(self, payload: Any) -> str | None:
        """Pull a human-readable message out of a platform error body."""
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        message = error or payload.get("message")
        if not isinstance(message, str):
            return None
        description = payload.get("error_description")
        if isinstance(description, str):
            message = f"{message} ({description})"
        return message

    def parse_json_object(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise UnexpectedResponseError(
                f"{self.name} answered HTTP {response.status_code} without a valid JSON object, "
                "so the outcome could not be confirmed",
                status_code=response.status_code,
            )
        return payload

    def require_post_id(self, payload: dict[str, Any], field: str = "id") -> str:
        post_id = payload.get(field)
        if post_id is None or isinstance(post_id, (dict, list, bool)) or str(post_id) == "":
            raise UnexpectedResponseError(
                f"{self.name} accepted the request but returned no post ID, "
                "so the publication could not be confirmed"
            )
        return str(post_id)

    def success(
        self,
        *,
        post_id: str | None,
        posted_url: str | None,
        public_access: bool | None,
        published_at: datetime | None,
        details: str | None = None,
    ) -> PlatformResult:
        notes = [details] if details else []
        if posted_url is None:
            notes.append(f"{self.name} did not return a public URL for the post.")
        if published_at is None:
            published_at = utc_now()
            notes.append(TIMESTAMP_FALLBACK_NOTE)
        date, time = split_date_time(published_at)
        return PlatformResult(
            platform=self.name,
            status=ResultStatus.SUCCESS,
            delivery_mode=self.delivery_mode,
            post_id=post_id,
            posted_url=posted_url,
            public_access=public_access,
            date=date,
            time=time,
            details=" ".join(notes) or None,
        )


def failed_result(
    publisher: Publisher, category: str, message: str, http_status: int | None = None
) -> PlatformResult:
    return PlatformResult(
        platform=publisher.name,
        status=ResultStatus.FAILED,
        delivery_mode=publisher.delivery_mode,
        error=PlatformError(category=category, message=message, http_status=http_status),
    )


def as_http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None
