import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TypeVar

from app.exceptions import PublishError

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    # A Retry-After longer than this is not worth holding the API request open for.
    max_retry_after: float = 30.0

    def backoff_delay(self, attempt: int) -> float:
        ceiling = min(self.max_delay, self.base_delay * (2**attempt))
        return random.uniform(ceiling / 2, ceiling)

    def delay_before_retry(self, attempt: int, retry_after: float | None) -> float | None:
        if retry_after is None:
            return self.backoff_delay(attempt)
        if retry_after > self.max_retry_after:
            return None
        return retry_after


async def call_with_retry(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    platform: str,
    request_id: str,
) -> T:
    """Run an operation, repeating it only for errors that are marked safe to retry."""
    attempt = 0
    while True:
        try:
            return await operation()
        except PublishError as error:
            if not error.retryable or attempt >= policy.max_retries:
                raise
            delay = policy.delay_before_retry(attempt, error.retry_after)
            if delay is None:
                raise
            attempt += 1
            logger.warning(
                "[%s] %s: %s error, retry %d/%d in %.2fs",
                request_id,
                platform,
                error.category,
                attempt,
                policy.max_retries,
                delay,
            )
            await asyncio.sleep(delay)


def parse_retry_after(header_value: str | None) -> float | None:
    """Read a Retry-After header, which holds either seconds or an HTTP date."""
    if not header_value:
        return None
    try:
        return max(float(header_value), 0.0)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
