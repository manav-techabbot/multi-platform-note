import asyncio
import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.core.logging import clean_remote_message
from app.core.retry import RetryPolicy, call_with_retry, parse_retry_after
from app.exceptions import (
    AuthenticationError,
    NetworkError,
    UnexpectedResponseError,
    error_from_status,
)
from app.oauth.token_store import OAuthTokens, TokenStore

logger = logging.getLogger(__name__)

SCOPES = ("basic", "write", "offline_access")
REAUTHORIZE_HINT = "Authorize again by opening /tumblr/authorize."


class TumblrOAuthClient:
    """Talks to Tumblr's OAuth 2.0 endpoints. Holds no tokens itself."""

    name = "Tumblr OAuth"

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http_client
        self.retry_policy = RetryPolicy(max_retries=settings.max_retries_for("tumblr"))

    def missing_settings(self) -> list[str]:
        required = {
            "TUMBLR_AUTHORIZE_URL": self.settings.tumblr_authorize_url,
            "TUMBLR_TOKEN_URL": self.settings.tumblr_token_url,
            "TUMBLR_CONSUMER_KEY": self.settings.tumblr_consumer_key,
            "TUMBLR_SECRET_KEY": self.settings.tumblr_secret_key,
            "TUMBLR_REDIRECT_URI": self.settings.tumblr_redirect_uri,
        }
        return [name for name, value in required.items() if not value]

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.settings.tumblr_consumer_key,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "state": state,
                "redirect_uri": self.settings.tumblr_redirect_uri,
            }
        )
        return f"{self.settings.tumblr_authorize_url}?{query}"

    async def exchange_code(self, code: str, request_id: str) -> OAuthTokens:
        # Codes are single-use, so a request that may have reached Tumblr is not repeated.
        return await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.settings.tumblr_consumer_key,
                "client_secret": self.settings.tumblr_secret_key,
                "redirect_uri": self.settings.tumblr_redirect_uri,
            },
            rejection="Tumblr rejected the authorization code. It may have expired or already "
            f"been used. {REAUTHORIZE_HINT}",
            request_id=request_id,
            repeatable=False,
        )

    async def refresh(self, refresh_token: str, request_id: str) -> OAuthTokens:
        return await self._request_tokens(
            {
                "grant_type": "refresh_token",
                "client_id": self.settings.tumblr_consumer_key,
                "client_secret": self.settings.tumblr_secret_key,
                "refresh_token": refresh_token,
            },
            rejection="Tumblr rejected the refresh token or client credentials. The refresh token may "
            f"be expired, revoked or already replaced. {REAUTHORIZE_HINT}",
            request_id=request_id,
            repeatable=True,
            previous_refresh_token=refresh_token,
        )

    async def _request_tokens(
        self,
        form: dict[str, str],
        *,
        rejection: str,
        request_id: str,
        repeatable: bool,
        previous_refresh_token: str = "",
    ) -> OAuthTokens:
        secrets = [value for key, value in form.items() if key != "grant_type" and value]
        secrets += self.settings.secret_values()

        async def attempt() -> OAuthTokens:
            response = await self._post(form, repeatable)
            if response.status_code in (400, 401, 403):
                detail = _describe_oauth_error(response, secrets)
                raise AuthenticationError(
                    f"{rejection} ({detail})" if detail else rejection,
                    status_code=response.status_code,
                )
            if not response.is_success:
                raise error_from_status(
                    response.status_code,
                    f"Tumblr token endpoint returned HTTP {response.status_code}",
                    idempotent=repeatable,
                    retry_after=parse_retry_after(response.headers.get("Retry-After")),
                )
            try:
                return OAuthTokens.from_token_response(response.json(), previous_refresh_token)
            except ValueError as error:
                raise UnexpectedResponseError(
                    f"Tumblr token endpoint returned an unusable response: {error}",
                    status_code=response.status_code,
                ) from None

        return await call_with_retry(
            attempt, self.retry_policy, platform=self.name, request_id=request_id
        )

    async def _post(self, form: dict[str, str], repeatable: bool) -> httpx.Response:
        try:
            return await self.http.post(self.settings.tumblr_token_url, data=form)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise NetworkError("Could not connect to Tumblr's token endpoint", retryable=True) from None
        except httpx.TransportError:
            raise NetworkError(
                "Tumblr's token endpoint did not answer in time", retryable=repeatable
            ) from None


class TumblrTokenManager:
    """Provides a valid Tumblr access token, refreshing and persisting tokens as needed.

    The token file is the source of truth because Tumblr can rotate the refresh token on
    every refresh, and the old one must not be used again. TUMBLR_ACCESS_TOKEN and
    TUMBLR_REFRESH_TOKEN only seed the manager until the first token file is written.
    """

    def __init__(self, oauth_client: TumblrOAuthClient, store: TokenStore) -> None:
        self.oauth = oauth_client
        self.store = store
        settings = oauth_client.settings
        self._seed = OAuthTokens(
            access_token=settings.tumblr_access_token,
            refresh_token=settings.tumblr_refresh_token,
        )
        self._tokens: OAuthTokens | None = None
        self._unsaved = False
        self._lock = asyncio.Lock()

    def current(self) -> OAuthTokens | None:
        stored = None if self._unsaved else self.store.load()
        if stored is not None:
            self._tokens = stored
        elif self._tokens is None and (self._seed.access_token or self._seed.refresh_token):
            self._tokens = self._seed
        return self._tokens

    def secrets(self) -> list[str]:
        tokens = self.current()
        if tokens is None:
            return []
        return [token for token in (tokens.access_token, tokens.refresh_token) if token]

    async def access_token(self, request_id: str) -> str:
        tokens = self.current()
        if tokens is None or not (tokens.access_token or tokens.refresh_token):
            raise AuthenticationError(f"No Tumblr tokens are available. {REAUTHORIZE_HINT}")
        if tokens.access_token and not tokens.is_expired():
            return tokens.access_token
        return await self.refresh(request_id, rejected_access_token=tokens.access_token)

    async def refresh(self, request_id: str, *, rejected_access_token: str) -> str:
        async with self._lock:
            tokens = self.current() or OAuthTokens()
            # Another request may have refreshed while this one waited for the lock.
            if (
                tokens.access_token
                and tokens.access_token != rejected_access_token
                and not tokens.is_expired()
            ):
                return tokens.access_token
            authorization_only = {"TUMBLR_REDIRECT_URI", "TUMBLR_AUTHORIZE_URL"}
            missing = [name for name in self.oauth.missing_settings() if name not in authorization_only]
            if not tokens.refresh_token:
                source = (
                    "TUMBLR_ACCESS_TOKEN from the environment"
                    if self.store.load() is None
                    else f"the access token in {self.store.path}"
                )
                message = (
                    f"Tumblr rejected {source} (expired, revoked, or not an OAuth 2.0 access "
                    "token) and no refresh token is available."
                )
                if missing:
                    message += f" Also missing: {', '.join(missing)}."
                raise AuthenticationError(f"{message} {REAUTHORIZE_HINT}", status_code=401)
            if missing:
                raise AuthenticationError(
                    "The Tumblr access token needs refreshing but these settings are missing: "
                    + ", ".join(missing)
                )
            refreshed = await self.oauth.refresh(tokens.refresh_token, request_id)
            self._remember(refreshed)
            logger.info("[%s] Tumblr access token refreshed", request_id)
            return refreshed.access_token

    def store_authorization(self, tokens: OAuthTokens) -> None:
        """Save tokens from a completed authorization. Raises OSError if they cannot be saved."""
        self.store.save(tokens)
        self._tokens = tokens
        self._unsaved = False

    def _remember(self, tokens: OAuthTokens) -> None:
        self._tokens = tokens
        try:
            self.store.save(tokens)
            self._unsaved = False
        except OSError as error:
            # Keep using the new tokens in memory; the old refresh token may already be dead.
            self._unsaved = True
            logger.error(
                "Could not save refreshed Tumblr tokens to %s (%s). They are kept in memory "
                "only and will be lost on restart.",
                self.store.path,
                error.strerror,
            )


def create_token_manager(settings: Settings, http_client: httpx.AsyncClient) -> TumblrTokenManager:
    return TumblrTokenManager(
        TumblrOAuthClient(settings, http_client), TokenStore(settings.tumblr_token_store_path)
    )


def _describe_oauth_error(response: httpx.Response, secrets: list[str]) -> str | None:
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    parts = [payload.get("error"), payload.get("error_description")]
    meta = payload.get("meta")
    if isinstance(meta, dict):
        parts.append(meta.get("msg"))
    text = ": ".join(part for part in parts if isinstance(part, str) and part)
    return clean_remote_message(text, secrets) if text else None

