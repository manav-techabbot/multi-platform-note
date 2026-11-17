import asyncio
import logging
import uuid

from app.publishers.base import Publisher, failed_result
from app.schemas import DeliveryMode, PlatformResult, PostingResponse, ResultStatus

logger = logging.getLogger(__name__)


class PostingService:
    def __init__(self, publishers: list[Publisher]) -> None:
        self.publishers = publishers

    async def publish(self, message: str, cta_url: str) -> PostingResponse:
        request_id = uuid.uuid4().hex
        logger.info(
            "[%s] Publishing to %d platform(s): %s",
            request_id,
            len(self.publishers),
            ", ".join(publisher.name for publisher in self.publishers),
        )
        results = await asyncio.gather(
            *(
                self._publish_safely(publisher, message, cta_url, request_id)
                for publisher in self.publishers
            )
        )
        return build_response(list(results))

    async def _publish_safely(
        self, publisher: Publisher, message: str, cta_url: str, request_id: str
    ) -> PlatformResult:
        try:
            return await publisher.publish(message, cta_url, request_id)
        except Exception:
            # Adapters turn expected failures into results themselves. Anything that still
            # escapes is a bug, and it must not take the other platforms down with it.
            logger.exception("[%s] Unexpected error in %s publisher", request_id, publisher.name)
            return failed_result(
                publisher,
                "internal_error",
                f"Unexpected internal error while publishing to {publisher.name}",
            )


def build_response(results: list[PlatformResult]) -> PostingResponse:
    real_publications = sum(result.is_real_publication for result in results)
    simulated_count = sum(
        result.delivery_mode is DeliveryMode.SIMULATED and result.status is ResultStatus.SUCCESS
        for result in results
    )
    success = real_publications > 0
    return PostingResponse(
        success=success,
        partial_success=success and real_publications < len(results),
        simulated_count=simulated_count,
        results=results,
    )
