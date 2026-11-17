from datetime import datetime, timedelta, timezone

import pytest

from app.utils.content_formatter import (
    derive_title,
    to_markdown,
    to_tumblr_content,
)
from app.utils.datetime_utils import parse_timestamp, split_date_time

URL = "https://example.com"


@pytest.mark.parametrize(
    "message, title",
    [
        ("Check out our latest business platform. https://example.com", "Check out our latest business platform"),
        ("Big news!  https://example.com", "Big news!"),
        ("Ready to grow? https://example.com", "Ready to grow?"),
        ("New   launch:\n https://example.com today", "New launch"),
        ("Sale - https://example.com", "Sale"),
        ("https://example.com is live", "Quick update"),
        ("... https://example.com", "Quick update"),
    ],
)
def test_derive_title(message, title):
    assert derive_title(message, URL) == title


def test_long_title_is_cut_at_a_word_boundary():
    message = "word " * 30 + URL

    title = derive_title(message, URL)

    assert title == "word " * 19 + "word..."
    assert len(title) <= 103


def test_title_is_deterministic():
    message = "Grow your online visibility today. https://example.com"

    assert derive_title(message, URL) == derive_title(message, URL)


def test_markdown_escapes_syntax_and_keeps_the_url_intact():
    message = "*Big* _news_ [here] # now `code` https://example.com/a_b_c?x=*1* <b>end</b>"
    url = "https://example.com/a_b_c?x=*1*"

    assert to_markdown(message, url) == (
        r"\*Big\* \_news\_ \[here\] \# now \`code\` "
        "<https://example.com/a_b_c?x=*1*>"
        r" \<b\>end\</b\>"
    )


@pytest.mark.parametrize(
    "message, expected",
    [
        ("- item https://example.com", r"\- item <https://example.com>"),
        ("+ item https://example.com", r"\+ item <https://example.com>"),
        ("1. first https://example.com", r"1\. first <https://example.com>"),
        ("e-commerce 2.0 https://example.com", "e-commerce 2.0 <https://example.com>"),
    ],
)
def test_markdown_does_not_turn_the_message_into_a_list(message, expected):
    assert to_markdown(message, URL) == expected


def test_tumblr_link_range_counts_code_points():
    message = "Café 👉 https://example.com now"

    block = to_tumblr_content(message, URL)[0]

    link = block["formatting"][0]
    assert block["text"] == message
    assert message[link["start"]:link["end"]] == URL
    assert link == {"start": 7, "end": 26, "type": "link", "url": URL}


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-17T10:30:00Z", datetime(2026, 11, 17, 10, 30, tzinfo=timezone.utc)),
        ("2026-11-17T10:30:00.123Z", datetime(2026, 11, 17, 10, 30, 0, 123000, tzinfo=timezone.utc)),
        ("2026-11-17T02:30:00-08:00", datetime(2026, 11, 17, 10, 30, tzinfo=timezone.utc)),
        ("2026-11-17T10:30:00", datetime(2026, 11, 17, 10, 30, tzinfo=timezone.utc)),
        ("2026-11-17 10:30:00 GMT", datetime(2026, 11, 17, 10, 30, tzinfo=timezone.utc)),
        (1794911400, datetime(2026, 11, 17, 10, 30, tzinfo=timezone.utc)),
    ],
)
def test_parse_timestamp(value, expected):
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-13-45T99:00:00Z", True, 10**20])
def test_unparseable_timestamps_return_none(value):
    assert parse_timestamp(value) is None


def test_split_date_time_converts_to_utc():
    india = timezone(timedelta(hours=5, minutes=30))

    assert split_date_time(datetime(2026, 11, 18, 1, 15, 30, tzinfo=india)) == ("2026-11-17", "19:45:30")
    assert split_date_time(None) == (None, None)
