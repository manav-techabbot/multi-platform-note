from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | int | float | None) -> datetime | None:
    """Parse an ISO 8601 string or Unix timestamp into an aware UTC datetime.

    Timestamps without an offset are taken as UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if text.endswith(" GMT") or text.endswith(" UTC"):
        text = text[:-4]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def split_date_time(moment: datetime | None) -> tuple[str | None, str | None]:
    if moment is None:
        return None, None
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M:%S")
