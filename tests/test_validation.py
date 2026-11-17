import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.schemas import PostingRequest
from app.utils.url_validation import UrlValidationError, extract_cta_url


def error_message(message: object) -> str:
    with pytest.raises(ValidationError) as caught:
        PostingRequest.model_validate({"message": message})
    return caught.value.errors()[0]["msg"]


def test_valid_message():
    request = PostingRequest(message="Check out our latest platform. https://example.com")

    assert request.message == "Check out our latest platform. https://example.com"
    assert request.cta_url == "https://example.com"


def test_missing_message():
    with pytest.raises(ValidationError) as caught:
        PostingRequest.model_validate({})

    assert caught.value.errors()[0]["type"] == "missing"


def test_empty_message():
    assert "at least 1 character" in error_message("")


def test_whitespace_only_message():
    assert "only whitespace" in error_message("   \n\t ")


def test_message_must_be_a_string():
    assert "valid string" in error_message(12345)


def test_message_of_exactly_200_characters_is_accepted():
    message = ("x" * 180 + " https://example.com").ljust(200, "!")

    assert len(PostingRequest(message=message).message) == 200


def test_message_longer_than_200_characters():
    message = "x" * 181 + " https://example.com"

    assert len(message) == 201
    assert "at most 200 characters" in error_message(message)


def test_message_without_url():
    assert "exactly one http or https URL" in error_message("Check out our platform today")


def test_message_with_two_urls():
    message = "Visit https://example.com or https://example.org today"

    assert "exactly one URL, found 2" in error_message(message)


def test_same_url_twice_counts_as_two_urls():
    assert "found 2" in error_message("https://example.com and again https://example.com")


@pytest.mark.parametrize(
    "message",
    [
        "Broken link https://",
        "Broken link https:///path-only",
        "Broken link http://exa_mple.com",
        "Broken link https://-example.com",
        "Broken link https://example..com",
        "Broken link https://example.com:99999",
        "Broken link https://example.c",
        "Broken link https://localhost/page",
        "Broken link https://[::1",
        "Two glued links https://a.example.comhttps://b.example.com",
    ],
)
def test_malformed_url(message):
    assert "URL" in error_message(message)


@pytest.mark.parametrize(
    "message, scheme",
    [
        ("Download from ftp://example.com/file", "ftp"),
        ("Open file://example.com/etc/passwd", "file"),
        ("Write to mailto:team@example.com", "mailto"),
        ("Click javascript:alert(1)", "javascript"),
        ("Join ws://example.com/socket", "ws"),
    ],
)
def test_unsupported_url_scheme(message, scheme):
    assert f"Unsupported URL scheme '{scheme}'" in error_message(message)


def test_url_without_scheme_is_not_absolute():
    assert "must be absolute" in error_message("Visit www.example.com today")


def test_unsupported_scheme_next_to_valid_url_is_rejected():
    assert "found 2" in error_message("See https://example.com and ftp://example.com/file")


@pytest.mark.parametrize(
    "message, expected_url",
    [
        ("Visit https://example.com.", "https://example.com"),
        ("Have you seen https://example.com/pricing?", "https://example.com/pricing"),
        ("New launch: https://example.com/a?b=1&c=2!", "https://example.com/a?b=1&c=2"),
        ("Our site (https://example.com/path) is live", "https://example.com/path"),
        ("Our site (see https://example.com).", "https://example.com"),
        ('He said "https://example.com/quote".', "https://example.com/quote"),
        ("Read https://en.wikipedia.org/wiki/Python_(language).", "https://en.wikipedia.org/wiki/Python_(language)"),
        ("Link:https://example.com, thanks", "https://example.com"),
        ("Docs at <https://example.com/docs>", "https://example.com/docs"),
        ("Trailing slash https://example.com/, ok", "https://example.com/"),
    ],
)
def test_trailing_punctuation_is_not_part_of_the_url(message, expected_url):
    assert PostingRequest(message=message).cta_url == expected_url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "HTTPS://EXAMPLE.COM/Path",
        "https://sub.domain.example.co.uk/a/b?c=d#frag",
        "https://example.com:8443/path",
        "http://192.168.1.10:8080/page",
        "https://münchen.de/straße",
        "https://xn--mnchen-3ya.de",
    ],
)
def test_accepted_url_forms(url):
    assert extract_cta_url(f"Take a look {url} now") == url


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError) as caught:
        PostingRequest.model_validate(
            {"message": "Hello https://example.com", "platforms": ["tumblr"]}
        )

    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_surrounding_whitespace_is_trimmed():
    assert PostingRequest(message="  Hello https://example.com \n").message == "Hello https://example.com"


def test_url_validation_error_is_a_value_error():
    with pytest.raises(UrlValidationError):
        extract_cta_url("no link here")
    assert issubclass(UrlValidationError, ValueError)


@pytest.fixture
def api_client():
    app = create_app(Settings(_env_file=None, enabled_platforms=""))
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": "No link in this message"},
        {"message": "Two links https://example.com https://example.org"},
        {"message": "Bad scheme ftp://example.com"},
        {"message": "x" * 201},
        {"message": "Hello https://example.com", "platform": "tumblr"},
    ],
)
def test_api_returns_422_for_invalid_requests(api_client, body):
    response = api_client.post("/posting", json=body)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "body"


def test_api_returns_422_for_malformed_json(api_client):
    response = api_client.post(
        "/posting", content="{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
