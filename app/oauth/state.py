import secrets
import time


class OAuthStateStore:
    """Single-use, short-lived `state` values that tie an OAuth callback to its request."""

    def __init__(self, ttl_seconds: float = 600, max_pending: int = 100) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self._pending: dict[str, float] = {}

    def issue(self) -> str:
        self._discard_expired()
        while len(self._pending) >= self.max_pending:
            oldest = min(self._pending, key=self._pending.__getitem__)
            del self._pending[oldest]
        state = secrets.token_urlsafe(32)
        self._pending[state] = time.monotonic() + self.ttl_seconds
        return state

    def consume(self, state: str) -> bool:
        self._discard_expired()
        return self._pending.pop(state, None) is not None

    def _discard_expired(self) -> None:
        now = time.monotonic()
        for state in [state for state, expires in self._pending.items() if expires <= now]:
            del self._pending[state]
