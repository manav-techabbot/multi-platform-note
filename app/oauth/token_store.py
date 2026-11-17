import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Refresh slightly early so a token does not expire while a request is in flight.
EXPIRY_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float | None = None
    scope: str = ""

    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at - EXPIRY_MARGIN_SECONDS

    @classmethod
    def from_token_response(cls, payload: Any, previous_refresh_token: str = "") -> "OAuthTokens":
        """Build tokens from an OAuth 2.0 token endpoint response, or raise ValueError."""
        if not isinstance(payload, dict):
            raise ValueError("token response is not a JSON object")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("token response has no access_token")

        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = previous_refresh_token
        expires_in = payload.get("expires_in")
        has_lifetime = isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
        scope = payload.get("scope")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in if has_lifetime else None,
            scope=scope if isinstance(scope, str) else "",
        )


class TokenStore:
    """Keeps OAuth tokens in a JSON file that only the current user can read."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> OAuthTokens | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            logger.warning("Could not read token store %s: %s", self.path, error.strerror)
            return None
        try:
            data = json.loads(raw)
            expires_at = data.get("expires_at")
            return OAuthTokens(
                access_token=str(data.get("access_token") or ""),
                refresh_token=str(data.get("refresh_token") or ""),
                expires_at=float(expires_at) if expires_at is not None else None,
                scope=str(data.get("scope") or ""),
            )
        except (ValueError, TypeError, AttributeError):
            logger.warning("Ignoring unreadable token store %s", self.path)
            return None

    def save(self, tokens: OAuthTokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        # Created with owner-only permissions before any secret is written. On Windows the
        # mode is ignored and the file inherits the folder's access rules.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump({**asdict(tokens), "updated_at": time.time()}, file, indent=2)
        # Replacing in one step means a crash never leaves a half-written token file.
        os.replace(temporary, self.path)
