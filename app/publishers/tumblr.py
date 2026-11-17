import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.exceptions import AuthenticationError, ConfigurationError, PublishError
from app.oauth.tumblr import create_token_manager
from app.publishers.base import Publisher, as_http_url
from app.schemas import PlatformResult
from app.utils.content_formatter import to_tumblr_content
from app.utils.datetime_utils import parse_timestamp

logger = logging.getLogger(__name__)



class TumblrPublisher(Publisher):
    key = "tumblr"
    name = "Tumblr"

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        super().__init__(settings, http_client)
        self.tokens = create_token_manager(settings, http_client)

    def missing_settings(self) -> list[str]:
        settings = self.settings
        required = {
            "TUMBLR_API_BASE_URL": settings.tumblr_api_base_url,
            "TUMBLR_BLOG_IDENTIFIER": settings.tumblr_blog_identifier,
        }
        missing = [name for name, value in required.items() if not value]
        tokens = self.tokens.current()
        if tokens is None:
            return missing + ["TUMBLR_ACCESS_TOKEN", "TUMBLR_REFRESH_TOKEN"]
        if not tokens.access_token or tokens.is_expired():
            refresh_settings = {
                "TUMBLR_TOKEN_URL": settings.tumblr_token_url,
                "TUMBLR_CONSUMER_KEY": settings.tumblr_consumer_key,
                "TUMBLR_SECRET_KEY": settings.tumblr_secret_key,
            }
            missing += [name for name, value in refresh_settings.items() if not value]
        return missing

    def check_configuration(self) -> None:
        missing = self.missing_settings()
        if not missing:
            return
        message = f"Tumblr is not configured. Missing settings: {', '.join(missing)}"
        if "TUMBLR_ACCESS_TOKEN" in missing:
            message += ". Set the tokens, or authorize by opening /tumblr/authorize"
        raise ConfigurationError(message)

    def secrets(self) -> list[str]:
        return super().secrets() + self.tokens.secrets()

    async def _authorized_request(
        self, method: str, url: str, *, request_id: str, **kwargs: Any
    ) -> httpx.Response:
        """Send a request with the current token, refreshing it once if Tumblr answers 401."""
        token = await self.tokens.access_token(request_id)
        try:
            return await self._send_with_token(token, method, url, request_id, kwargs)
        except AuthenticationError:
            token = await self.tokens.refresh(request_id, rejected_access_token=token)
        return await self._send_with_token(token, method, url, request_id, kwargs)

    async def _send_with_token(
        self, token: str, method: str, url: str, request_id: str, kwargs: dict[str, Any]
    ) -> httpx.Response:
        headers = {**kwargs.get("headers", {}), "Authorization": f"Bearer {token}"}
        return await self.request(
            method, url, request_id=request_id, **{**kwargs, "headers": headers}
        )

    @property
    def _posts_url(self) -> str:
        blog = quote(self.settings.tumblr_blog_identifier, safe="")
        return f"{self.settings.tumblr_api_base_url.rstrip('/')}/blog/{blog}/posts"

    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        response = await self._authorized_request(
            "POST",
            self._posts_url,
            request_id=request_id,
            json={"content": to_tumblr_content(message, cta_url), "state": "published"},
        )
        created = self._response_body(response)
        post_id = self.require_post_id(created)

        # The create call only returns the post ID, so the public URL, state and
        # timestamp have to be read back from the blog.
        post = await self._fetch_post(post_id, request_id)
        if post is None:
            return self.success(
                post_id=post_id,
                posted_url=None,
                public_access=None,
                published_at=None,
                details="Tumblr created the post but it could not be read back to confirm "
                "its public URL.",
            )

        state = post.get("state")
        is_published = state == "published" if state else None
        return self.success(
            post_id=post_id,
            posted_url=as_http_url(post.get("post_url")) if is_published is not False else None,
            public_access=is_published,
            published_at=parse_timestamp(post.get("timestamp")),
            details=None if is_published is not False else f"Tumblr post state is '{state}'.",
        )

    async def _fetch_post(self, post_id: str, request_id: str) -> dict[str, Any] | None:
        try:
            response = await self._authorized_request(
                "GET",
                self._posts_url,
                request_id=request_id,
                idempotent=True,
                params={"id": post_id, "npf": "true"},
            )
            posts = self._response_body(response).get("posts")
        except PublishError as error:
            logger.warning(
                "[%s] Tumblr post %s was created but could not be read back: %s",
                request_id,
                post_id,
                error.message,
            )
            return None
        if isinstance(posts, list) and posts and isinstance(posts[0], dict):
            return posts[0]
        return None

    def _response_body(self, response: httpx.Response) -> dict[str, Any]:
        body = self.parse_json_object(response).get("response")
        return body if isinstance(body, dict) else {}

    def extract_error_message(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            message = errors[0].get("detail") or errors[0].get("title")
            if isinstance(message, str):
                return message
        meta = payload.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("msg"), str):
            return meta["msg"]
        return super().extract_error_message(payload)
