from app.exceptions import UnexpectedResponseError
from app.publishers.base import Publisher
from app.schemas import DeliveryMode, PlatformResult, ResultStatus
from app.utils.content_formatter import derive_title


class JsonPlaceholderPublisher(Publisher):
    key = "jsonplaceholder"
    name = "JSONPlaceholder"
    delivery_mode = DeliveryMode.SIMULATED
    # Nothing is stored server-side, so a repeated request cannot duplicate anything.
    idempotent = True

    def missing_settings(self) -> list[str]:
        return [] if self.settings.jsonplaceholder_base_url else ["JSONPLACEHOLDER_BASE_URL"]

    async def _publish(self, message: str, cta_url: str, request_id: str) -> PlatformResult:
        payload = {
            "title": derive_title(message, cta_url),
            "body": message,
            "userId": self.settings.jsonplaceholder_user_id,
        }
        response = await self.request(
            "POST",
            f"{self.settings.jsonplaceholder_base_url.rstrip('/')}/posts",
            request_id=request_id,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json=payload,
        )
        echoed = self.parse_json_object(response)
        simulated_id = self.require_post_id(echoed)
        for field, sent_value in payload.items():
            if echoed.get(field) != sent_value:
                raise UnexpectedResponseError(
                    f"JSONPlaceholder did not echo the submitted '{field}' field"
                )

        return PlatformResult(
            platform=self.name,
            status=ResultStatus.SUCCESS,
            delivery_mode=self.delivery_mode,
            posted_url=None,
            public_access=False,
            details=(
                f"Simulated POST accepted (placeholder id {simulated_id}). "
                "The resource is not persisted or publicly accessible."
            ),
        )
