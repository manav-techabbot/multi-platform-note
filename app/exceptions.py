class PublishError(Exception):
    """A platform publishing failure, already safe to show to API consumers."""

    category = "publish_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


class ConfigurationError(PublishError):
    category = "configuration"


class AuthenticationError(PublishError):
    category = "authentication"


class PermissionDeniedError(PublishError):
    category = "permission_denied"


class InvalidRequestError(PublishError):
    category = "invalid_request"


class ResourceNotFoundError(PublishError):
    category = "not_found"


class ConflictError(PublishError):
    category = "conflict"


class ContentRejectedError(PublishError):
    category = "content_rejected"


class RateLimitError(PublishError):
    category = "rate_limited"


class PlatformUnavailableError(PublishError):
    category = "platform_unavailable"


class NetworkError(PublishError):
    category = "network"


class UncertainDeliveryError(PublishError):
    """The request was sent but no answer arrived, so the post may or may not exist."""

    category = "uncertain_delivery"


class UnexpectedResponseError(PublishError):
    category = "unexpected_response"


_STATUS_ERRORS: dict[int, type[PublishError]] = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: ResourceNotFoundError,
    409: ConflictError,
    422: ContentRejectedError,
    429: RateLimitError,
}


def error_from_status(
    status_code: int,
    message: str,
    *,
    idempotent: bool,
    retry_after: float | None = None,
) -> PublishError:
    if status_code >= 500:
        # A 503 means the request was turned away. Any other 5xx may have been raised
        # after the post was stored, so it is only repeated when a duplicate is impossible.
        return PlatformUnavailableError(
            message, status_code=status_code, retryable=idempotent or status_code == 503
        )
    error_class = _STATUS_ERRORS.get(status_code, UnexpectedResponseError)
    return error_class(
        message,
        status_code=status_code,
        retryable=status_code == 429,
        retry_after=retry_after,
    )
