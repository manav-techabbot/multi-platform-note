import re

FALLBACK_TITLE = "Quick update"
MAX_TITLE_LENGTH = 100

_MARKDOWN_SPECIAL_CHARS = re.compile(r"([\\`*_\[\]<>#~|])")
_MARKDOWN_BULLET = re.compile(r"^(\s*)([-+])(\s)", re.MULTILINE)
_MARKDOWN_NUMBERED = re.compile(r"^(\s*\d+)\.(\s)", re.MULTILINE)


def split_around_url(message: str, cta_url: str) -> tuple[str, str]:
    before, _, after = message.partition(cta_url)
    return before, after


def derive_title(message: str, cta_url: str) -> str:
    before, _ = split_around_url(message, cta_url)
    title = " ".join(before.split()).rstrip(" .,:;-–—")
    if not title:
        return FALLBACK_TITLE
    if len(title) > MAX_TITLE_LENGTH:
        title = title[:MAX_TITLE_LENGTH].rsplit(" ", 1)[0].rstrip(" .,:;-–—") + "..."
    return title


def to_markdown(message: str, cta_url: str) -> str:
    """Escape Markdown syntax so the text renders literally, with the URL as an autolink."""
    before, after = split_around_url(message, cta_url)
    return f"{_escape_markdown(before)}<{cta_url}>{_escape_markdown(after)}"


def to_tumblr_content(message: str, cta_url: str) -> list[dict]:
    # NPF formatting ranges are counted in Unicode code points, the same unit as str indices.
    start = message.index(cta_url)
    return [
        {
            "type": "text",
            "text": message,
            "formatting": [
                {"start": start, "end": start + len(cta_url), "type": "link", "url": cta_url}
            ],
        }
    ]


def _escape_markdown(text: str) -> str:
    text = _MARKDOWN_SPECIAL_CHARS.sub(r"\\\1", text)
    text = _MARKDOWN_BULLET.sub(r"\1\\\2\3", text)
    return _MARKDOWN_NUMBERED.sub(r"\1\\.\2", text)
