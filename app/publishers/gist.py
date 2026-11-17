import time

import httpx

from app.core.retry import parse_retry_after
from app.exceptions import PublishError, RateLimitError
from app.publishers.base import Publisher, as_http_url
from app.schemas import PlatformResult
from app.utils.content_formatter import derive_title, to_markdown
from app.utils.datetime_utils import parse_timestamp

# GitHub renders .md gist files, so the escaped Markdown shows the message with a clickable link.
# Names like "gistfile1" are reserved by GitHub and must not be used.
GIST_FILENAME = "message.md"


class GistPublisher(Publisher):
    key = "gist"
    name = "GitHub Gist"

    def missing_settings(self) -> list[str]:
        required = {
            "GITHUB_API_BASE_URL": self.settings.github_api_base_url,
            "GITHUB_API_VERSION": self.settings.github_api_version,
            "GITHUB_GIST_TOKEN": self.settings.github_gist_token,
        }
        return [name for name, value in required.items() if not value]

    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        response = await self.request(
            "POST",
            f"{self.settings.github_api_base_url.rstrip('/')}/gists",
            request_id=request_id,
            headers={
                "Authorization": f"Bearer {self.settings.github_gist_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.settings.github_api_version,
            },
            json={
                "description": derive_title(message, cta_url),
                "public": self.settings.github_gist_public,
                "files": {GIST_FILENAME: {"content": to_markdown(message, cta_url)}},
            },
        )
        gist = self.parse_json_object(response)
        gist_id = self.require_post_id(gist)
        is_public = gist.get("public")
        if not isinstance(is_public, bool):
            is_public = None

        return self.success(
            post_id=gist_id,
            posted_url=as_http_url(gist.get("html_url")),
            public_access=is_public,
            published_at=parse_timestamp(gist.get("created_at")),
            details=(
                "Secret gist: not listed or searchable, but anyone with the URL can open it."
                if is_public is False
                else None
            ),
        )

    def classify_error_response(self, response: httpx.Response, idempotent: bool) -> PublishError:
        # GitHub reports rate limits as 403 (or 429) with rate-limit headers rather than 429 alone.
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        out_of_quota = response.headers.get("X-RateLimit-Remaining") == "0"
        if response.status_code in (403, 429) and (out_of_quota or retry_after is not None):
            if retry_after is None:
                reset_at = response.headers.get("X-RateLimit-Reset", "")
                retry_after = max(float(reset_at) - time.time(), 0) if reset_at.isdigit() else None
            return RateLimitError(
                self._describe_error_response(response),
                status_code=response.status_code,
                retryable=True,
                retry_after=retry_after,
            )
        return super().classify_error_response(response, idempotent)
