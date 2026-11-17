"""End-to-end tests of POST /posting with every adapter and mocked platform APIs."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MESSAGE, SECRETS, make_settings
from tests.platforms.test_bluesky import CREATED as BLUESKY_CREATED
from tests.platforms.test_bluesky import DID, XRPC_URL, session_response
from tests.platforms.test_devto import ARTICLE, ARTICLES_URL
from tests.platforms.test_gist import GIST, GISTS_URL
from tests.platforms.test_jsonplaceholder import POSTS_URL as JSONPLACEHOLDER_URL
from tests.platforms.test_jsonplaceholder import SENT as JSONPLACEHOLDER_PAYLOAD
from tests.platforms.test_tumblr import CREATED as TUMBLR_CREATED
from tests.platforms.test_tumblr import FETCHED as TUMBLR_FETCHED
from tests.platforms.test_tumblr import POSTS_URL as TUMBLR_URL

PLATFORM_NAMES = [
    "DEV Community",
    "Bluesky",
    "Tumblr",
    "GitHub Gist",
    "JSONPlaceholder",
]
RESULT_FIELDS = {
    "platform",
    "status",
    "delivery_mode",
    "post_id",
    "posted_url",
    "public_access",
    "date",
    "time",
    "error",
    "details",
}


@pytest.fixture
def platforms(mock_api):
    mock_api.post(f"{XRPC_URL}/com.atproto.server.createSession").mock(return_value=session_response())
    mock_api.get(f"{XRPC_URL}/app.bsky.actor.getProfile").mock(
        return_value=httpx.Response(200, json={"did": DID, "handle": "tester.bsky.social"})
    )
    mock_api.get(TUMBLR_URL).mock(return_value=httpx.Response(200, json=TUMBLR_FETCHED))
    return {
        "DEV Community": mock_api.post(ARTICLES_URL).mock(return_value=httpx.Response(201, json=ARTICLE)),
        "Bluesky": mock_api.post(f"{XRPC_URL}/com.atproto.repo.createRecord").mock(
            return_value=httpx.Response(200, json=BLUESKY_CREATED)
        ),
        "Tumblr": mock_api.post(TUMBLR_URL).mock(return_value=httpx.Response(201, json=TUMBLR_CREATED)),
        "GitHub Gist": mock_api.post(GISTS_URL).mock(return_value=httpx.Response(201, json=GIST)),
        "JSONPlaceholder": mock_api.post(JSONPLACEHOLDER_URL).mock(
            return_value=httpx.Response(201, json={**JSONPLACEHOLDER_PAYLOAD, "id": 101})
        ),
    }


@pytest.fixture
def client():
    with TestClient(create_app(make_settings())) as test_client:
        yield test_client


def test_message_is_published_to_every_platform(client, platforms):
    response = client.post("/posting", json={"message": MESSAGE})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"success", "partial_success", "simulated_count", "results"}
    assert body["success"] is True
    assert body["partial_success"] is True
    assert body["simulated_count"] == 1
    assert [result["platform"] for result in body["results"]] == PLATFORM_NAMES

    for result in body["results"]:
        assert set(result) == RESULT_FIELDS
        assert result["status"] == "success"
        assert result["error"] is None

    for result in body["results"][:4]:
        assert result["delivery_mode"] == "published"
        assert result["posted_url"].startswith("https://")
        assert result["public_access"] is True
        assert result["date"] == "2026-11-17" or result["platform"] == "Bluesky"
        assert result["time"] is not None

    simulated = body["results"][4]
    assert simulated["delivery_mode"] == "simulated"
    assert simulated["posted_url"] is None
    assert simulated["public_access"] is False
    assert simulated["date"] is None and simulated["time"] is None
    assert "not persisted" in simulated["details"]

    for name, route in platforms.items():
        assert route.call_count == 1, name
    for secret in SECRETS.values():
        assert secret not in response.text


def test_partial_failure_keeps_other_platforms_publishing(client, platforms):
    platforms["GitHub Gist"].mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    platforms["DEV Community"].mock(side_effect=httpx.ReadTimeout("no answer"))
    platforms["Tumblr"].mock(return_value=httpx.Response(403, json={"meta": {"status": 403, "msg": "Forbidden"}}))

    response = client.post("/posting", json={"message": MESSAGE})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["partial_success"] is True
    outcome = {result["platform"]: result for result in body["results"]}
    assert outcome["GitHub Gist"]["error"]["category"] == "authentication"
    assert outcome["DEV Community"]["error"]["category"] == "uncertain_delivery"
    assert outcome["Tumblr"]["error"]["category"] == "permission_denied"
    for name in ("Bluesky", "JSONPlaceholder"):
        assert outcome[name]["status"] == "success"


def test_only_simulated_success_is_not_an_overall_success(client, platforms):
    for name, route in platforms.items():
        if name != "JSONPlaceholder":
            route.mock(return_value=httpx.Response(403, json={"error": "Forbidden"}))

    response = client.post("/posting", json={"message": MESSAGE})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["partial_success"] is False
    assert body["simulated_count"] == 1
    assert [result["status"] for result in body["results"]] == ["failed"] * 4 + ["success"]


def test_posting_is_the_only_publishing_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert {path: list(methods) for path, methods in paths.items()} == {
        "/posting": ["post"],
        "/tumblr/authorize": ["get"],
        "/tumblr/callback": ["get"],
    }
    assert client.get("/posting").status_code == 405
