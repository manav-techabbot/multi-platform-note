import hmac
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.exceptions import AuthenticationError, PublishError
from app.oauth.state import OAuthStateStore
from app.oauth.tumblr import SCOPES, TumblrOAuthClient, TumblrTokenManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tumblr", tags=["Tumblr OAuth"])

STATE_COOKIE = "tumblr_oauth_state"


class TumblrAuthorizationResult(BaseModel):
    status: str
    scope: str
    missing_scopes: list[str]
    refresh_token_received: bool
    access_token_expires_at: float | None


@router.get(
    "/authorize",
    summary="Start the Tumblr OAuth 2.0 authorization",
    response_class=RedirectResponse,
    status_code=302,
)
async def authorize(request: Request) -> Response:
    oauth: TumblrOAuthClient = request.app.state.tumblr_oauth
    missing = oauth.missing_settings()
    if missing:
        return JSONResponse(
            status_code=503,
            content={"detail": f"Tumblr OAuth is not configured. Missing settings: {', '.join(missing)}"},
        )

    states: OAuthStateStore = request.app.state.tumblr_oauth_states
    state = states.issue()
    response = RedirectResponse(oauth.authorization_url(state), status_code=302)
    # The cookie binds the state to this browser, so a callback link produced in someone
    # else's browser cannot complete an authorization here.
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=int(states.ttl_seconds),
        path="/tumblr",
        httponly=True,
        samesite="lax",
        secure=oauth.settings.tumblr_redirect_uri.startswith("https://"),
    )
    return response


@router.get(
    "/callback",
    summary="Complete the Tumblr OAuth 2.0 authorization",
    response_model=TumblrAuthorizationResult,
    responses={
        400: {"description": "Invalid state or authorization refused"},
        502: {"description": "Token exchange failed"},
    },
)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    states: OAuthStateStore = request.app.state.tumblr_oauth_states
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    state_matches_browser = bool(state) and hmac.compare_digest(state, cookie_state)
    if not state or not states.consume(state) or not state_matches_browser:
        return _failure(
            400, "Invalid or expired OAuth state. Start again by opening /tumblr/authorize."
        )
    if error:
        reason = f"{error}: {error_description}" if error_description else error
        return _failure(400, f"Tumblr authorization was not granted ({reason[:200]}).")
    if not code:
        return _failure(400, "The callback did not include an authorization code.")

    oauth: TumblrOAuthClient = request.app.state.tumblr_oauth
    tokens_manager: TumblrTokenManager = request.app.state.tumblr_tokens
    request_id = uuid.uuid4().hex
    try:
        tokens = await oauth.exchange_code(code, request_id)
    except AuthenticationError as rejection:
        return _failure(400, rejection.message)
    except PublishError as failure:
        logger.warning("[%s] Tumblr code exchange failed: %s", request_id, failure.message)
        return _failure(502, failure.message)

    try:
        tokens_manager.store_authorization(tokens)
    except OSError as save_error:
        logger.error(
            "[%s] Tumblr tokens could not be saved to %s: %s",
            request_id,
            tokens_manager.store.path,
            save_error.strerror,
        )
        return _failure(500, "Tumblr tokens were received but could not be saved. Check TUMBLR_TOKEN_STORE_PATH.")

    granted = set(tokens.scope.split())
    logger.info("[%s] Tumblr authorization completed", request_id)
    response = JSONResponse(
        TumblrAuthorizationResult(
            status="authorized",
            scope=tokens.scope,
            missing_scopes=[scope for scope in SCOPES if granted and scope not in granted],
            refresh_token_received=bool(tokens.refresh_token),
            access_token_expires_at=tokens.expires_at,
        ).model_dump()
    )
    response.delete_cookie(STATE_COOKIE, path="/tumblr")
    return response


def _failure(status_code: int, detail: str) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    response.delete_cookie(STATE_COOKIE, path="/tumblr")
    return response
