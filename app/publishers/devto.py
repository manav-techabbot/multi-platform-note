from app.publishers.base import Publisher, as_http_url
from app.schemas import PlatformResult
from app.utils.content_formatter import derive_title, to_markdown
from app.utils.datetime_utils import parse_timestamp


class DevtoPublisher(Publisher):
    key = "devto"
    name = "DEV Community"

    def missing_settings(self) -> list[str]:
        required = {
            "DEVTO_BASE_URL": self.settings.devto_base_url,
            "DEVTO_API_KEY": self.settings.devto_api_key,
        }
        return [name for name, value in required.items() if not value]

    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        response = await self.request(
            "POST",
            f"{self.settings.devto_base_url.rstrip('/')}/api/articles",
            request_id=request_id,
            headers={
                "api-key": self.settings.devto_api_key,
                "Accept": "application/vnd.forem.api-v1+json",
            },
            json={
                "article": {
                    "title": derive_title(message, cta_url),
                    "body_markdown": to_markdown(message, cta_url),
                    "published": True,
                }
            },
        )
        article = self.parse_json_object(response)
        post_id = self.require_post_id(article)
        published_at = parse_timestamp(
            article.get("published_at") or article.get("published_timestamp")
        )
        posted_url = as_http_url(article.get("url"))

        return self.success(
            post_id=post_id,
            posted_url=posted_url,
            # DEV only sets a publication timestamp once the article is live.
            public_access=True if posted_url and published_at else None,
            published_at=published_at,
        )
