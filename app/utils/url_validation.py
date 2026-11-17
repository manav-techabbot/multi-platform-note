import ipaddress
import re
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
NON_WEB_SCHEME_PREFIXES = ("mailto:", "javascript:", "data:", "tel:", "file:")

_SCHEME_START = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
_TRAILING_PUNCTUATION = ".,;:!?'\"*"
_BRACKET_PAIRS = {")": "(", "]": "[", "}": "{", ">": "<"}


class UrlValidationError(ValueError):
    pass


def extract_cta_url(message: str) -> str:
    """Return the single http(s) URL in the message, exactly as it is written there."""
    candidates = _find_url_candidates(message)
    if not candidates:
        raise UrlValidationError("Message must contain exactly one http or https URL")
    if len(candidates) > 1:
        raise UrlValidationError(
            f"Message must contain exactly one URL, found {len(candidates)}"
        )
    url = candidates[0]
    validate_http_url(url)
    return url


def validate_http_url(url: str) -> None:
    if url.count("://") > 1:
        raise UrlValidationError(f"Malformed URL: {url}")
    try:
        parts = urlsplit(url)
        # Reading the port is what makes urllib reject a non-numeric or out-of-range one.
        _ = parts.port
    except ValueError:
        raise UrlValidationError(f"Malformed URL: {url}") from None

    if not parts.scheme:
        raise UrlValidationError(f"URL must be absolute and start with http:// or https://: {url}")
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlValidationError(
            f"Unsupported URL scheme '{parts.scheme}': only http and https URLs are accepted"
        )
    if not parts.hostname or not is_valid_hostname(parts.hostname):
        raise UrlValidationError(f"URL has an invalid hostname: {url}")


def is_valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False

    if len(ascii_hostname) > 253:
        return False
    labels = ascii_hostname.rstrip(".").split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        return False
    top_level = labels[-1]
    return len(top_level) >= 2 and (top_level.isalpha() or top_level.startswith("xn--"))


def _find_url_candidates(message: str) -> list[str]:
    candidates = []
    for token in message.split():
        scheme_match = _SCHEME_START.search(token)
        if scheme_match:
            candidates.append(_strip_trailing_punctuation(token[scheme_match.start():]))
            continue
        bare = token.lstrip("([{<\"'")
        if bare.lower().startswith(("www.", *NON_WEB_SCHEME_PREFIXES)):
            candidates.append(_strip_trailing_punctuation(bare))
    return candidates


def _strip_trailing_punctuation(candidate: str) -> str:
    while candidate:
        last = candidate[-1]
        if last in _TRAILING_PUNCTUATION:
            candidate = candidate[:-1]
        elif last in _BRACKET_PAIRS and candidate.count(last) > candidate.count(_BRACKET_PAIRS[last]):
            # "(see https://example.com)" ends the sentence, while a Wikipedia-style
            # "https://example.com/Foo_(bar)" keeps its balanced bracket.
            candidate = candidate[:-1]
        else:
            break
    return candidate
