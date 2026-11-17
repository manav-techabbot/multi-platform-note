import logging

REDACTED = "***"
MAX_REMOTE_MESSAGE_LENGTH = 300


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request line at INFO, which is noise next to our own publish logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def clean_remote_message(text: str, secrets: list[str]) -> str:
    """Make text returned by a third-party API safe and short enough to show to callers."""
    text = " ".join(redact(text, secrets).split())
    if len(text) > MAX_REMOTE_MESSAGE_LENGTH:
        text = text[:MAX_REMOTE_MESSAGE_LENGTH] + "..."
    return text
