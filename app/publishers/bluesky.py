import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from atproto import AsyncClient, AtUri, client_utils
from atproto_client.exceptions import RequestErrorBase
from atproto_client.models.common import XrpcError
from atproto_client.request import AsyncRequest
from atproto_core.exceptions import AtProtocolError

from app.config import Settings
from app.core.logging import clean_remote_message
from app.core.retry import call_with_retry
from app.exceptions import (
    AuthenticationError,
    NetworkError,
    PublishError,
    UncertainDeliveryError,
    UnexpectedResponseError,
    error_from_status,
)
from app.publishers.base import Publisher
from app.schemas import PlatformResult
from app.utils.content_formatter import split_around_url
from app.utils.datetime_utils import utc_now

POST_COLLECTION = "app.bsky.feed.post"

T = TypeVar("T")


class BlueskyPublisher(Publisher):
    key = "bluesky"
    name = "Bluesky"

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        super().__init__(settings, http_client)
        # The SDK owns its HTTP session, so it gets its own client with the same timeouts.
        self._client: AsyncClient | None = None
        self._login_lock = asyncio.Lock()

    def missing_settings(self) -> list[str]:
        required = {
            "BLUESKY_SERVICE_URL": self.settings.bluesky_service_url,
            "BLUESKY_WEB_URL": self.settings.bluesky_web_url,
            "BLUESKY_HANDLE": self.settings.bluesky_handle,
            "BLUESKY_APP_PASSWORD": self.settings.bluesky_app_password,
        }
        return [name for name, value in required.items() if not value]

    def create_client(self) -> AsyncClient:
        timeout = httpx.Timeout(
            self.settings.request_timeout, connect=self.settings.connect_timeout
        )
        return AsyncClient(
            base_url=self.settings.bluesky_service_url, request=AsyncRequest(timeout=timeout)
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.request.close()
            self._client = None

    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        before, after = split_around_url(message, cta_url)
        post_text = client_utils.TextBuilder().text(before).link(cta_url, cta_url).text(after)

        client = await self._logged_in_client(request_id)
        sent_at = utc_now()
        try:
            created = await self._send_post(client, post_text, request_id)
        except AuthenticationError:
            # The cached session is no longer accepted. A 401 means nothing was posted,
            # so logging in again and sending once more is safe.
            await self.aclose()
            client = await self._logged_in_client(request_id)
            sent_at = utc_now()
            created = await self._send_post(client, post_text, request_id)

        uri, cid = getattr(created, "uri", None), getattr(created, "cid", None)
        if not uri or not cid:
            raise UnexpectedResponseError(
                "Bluesky accepted the request but returned no record URI or CID, "
                "so the publication could not be confirmed"
            )
        posted_url = self._public_url(uri)
        return self.success(
            post_id=uri,
            posted_url=posted_url,
            public_access=True if posted_url else None,
            # createRecord returns only the URI and CID. The record's createdAt is set by
            # the SDK at send time, which is the moment captured here.
            published_at=sent_at,
            details=f"AT URI {uri}, CID {cid}.",
        )

    async def _logged_in_client(self, request_id: str) -> AsyncClient:
        async with self._login_lock:
            if self._client is not None:
                return self._client
            client = self.create_client()
            try:
                await self._call_sdk(
                    lambda: client.login(
                        self.settings.bluesky_handle, self.settings.bluesky_app_password
                    ),
                    request_id,
                    idempotent=True,
                )
            except PublishError:
                await client.request.close()
                raise
            self._client = client
            return client

    async def _send_post(
        self, client: AsyncClient, post_text: client_utils.TextBuilder, request_id: str
    ):
        return await self._call_sdk(
            lambda: client.send_post(post_text), request_id, idempotent=False
        )

    async def _call_sdk(
        self, operation: Callable[[], Awaitable[T]], request_id: str, *, idempotent: bool
    ) -> T:
        async def attempt() -> T:
            try:
                return await operation()
            except RequestErrorBase as error:
                raise self._translate_sdk_error(error, idempotent) from None
            except AtProtocolError as error:
                raise UnexpectedResponseError(
                    f"Bluesky SDK could not complete the request ({type(error).__name__})"
                ) from None

        return await call_with_retry(
            attempt, self.retry_policy, platform=self.name, request_id=request_id
        )

    def _translate_sdk_error(self, error: RequestErrorBase, idempotent: bool) -> PublishError:
        response = error.response
        if response is None:
            if isinstance(error.__cause__, (httpx.ConnectError, httpx.ConnectTimeout)):
                return NetworkError("Could not connect to Bluesky", retryable=True)
            if idempotent:
                return NetworkError("The request to Bluesky was interrupted", retryable=True)
            return UncertainDeliveryError(
                "The request to Bluesky timed out or was interrupted after it was sent. "
                "The post may or may not have been created, so it was not retried. "
                "Check the account before posting again."
            )

        description = f"Bluesky returned HTTP {response.status_code}"
        if isinstance(response.content, XrpcError):
            remote_message = response.content.message or response.content.error
            if remote_message:
                description += f": {clean_remote_message(remote_message, self.secrets())}"
        return error_from_status(
            response.status_code,
            description,
            idempotent=idempotent,
            retry_after=getattr(error, "retry_after", None),
        )

    def _public_url(self, uri: str) -> str | None:
        try:
            at_uri = AtUri.from_str(uri)
        except Exception:
            return None
        if at_uri.collection != POST_COLLECTION or not at_uri.rkey:
            return None
        handle = self.settings.bluesky_handle.lstrip("@")
        # Accounts can also log in with an email address, which is not a profile identifier.
        profile = at_uri.hostname if "@" in handle else handle
        return f"{self.settings.bluesky_web_url.rstrip('/')}/profile/{profile}/post/{at_uri.rkey}"
