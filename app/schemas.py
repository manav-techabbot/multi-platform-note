from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.url_validation import extract_cta_url

MAX_MESSAGE_LENGTH = 200


class PostingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH,
        description="Short message containing exactly one http(s) call-to-action URL.",
        examples=["Check out our latest business platform. https://example.com"],
    )

    @field_validator("message")
    @classmethod
    def check_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("Message cannot be empty or contain only whitespace")
        extract_cta_url(message)
        return message

    @property
    def cta_url(self) -> str:
        return extract_cta_url(self.message)


class ResultStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class DeliveryMode(str, Enum):
    PUBLISHED = "published"
    SIMULATED = "simulated"


class PlatformError(BaseModel):
    category: str
    message: str
    http_status: int | None = None


class PlatformResult(BaseModel):
    platform: str
    status: ResultStatus
    delivery_mode: DeliveryMode
    post_id: str | None = None
    posted_url: str | None = None
    public_access: bool | None = None
    date: str | None = Field(default=None, description="Publication date in UTC, YYYY-MM-DD.")
    time: str | None = Field(default=None, description="Publication time in UTC, HH:MM:SS.")
    error: PlatformError | None = None
    details: str | None = None

    @property
    def is_real_publication(self) -> bool:
        return (
            self.status is ResultStatus.SUCCESS
            and self.delivery_mode is DeliveryMode.PUBLISHED
            and self.public_access is not False
        )


class PostingResponse(BaseModel):
    success: bool = Field(description="True when at least one real platform published the message.")
    partial_success: bool = Field(
        description="True when a real publication succeeded alongside failed or simulated results."
    )
    simulated_count: int
    results: list[PlatformResult]
