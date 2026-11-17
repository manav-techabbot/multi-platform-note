import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.posting import router as posting_router
from app.api.tumblr_oauth import router as tumblr_oauth_router
from app.config import Settings, get_settings
from app.core.http_client import create_http_client
from app.core.logging import configure_logging
from app.oauth.state import OAuthStateStore
from app.oauth.tumblr import create_token_manager
from app.publishers.registry import build_publishers
from app.services.posting_service import PostingService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app_settings = settings or get_settings()
        configure_logging(app_settings.log_level)
        http_client = create_http_client(app_settings)
        publishers = build_publishers(app_settings, http_client)
        app.state.posting_service = PostingService(publishers)
        app.state.tumblr_tokens = create_token_manager(app_settings, http_client)
        app.state.tumblr_oauth = app.state.tumblr_tokens.oauth
        app.state.tumblr_oauth_states = OAuthStateStore()
        logger.info("Enabled platforms: %s", ", ".join(p.name for p in publishers) or "none")
        try:
            yield
        finally:
            for publisher in publishers:
                await publisher.aclose()
            await http_client.aclose()

    app = FastAPI(
        title="Multiplatform note",
        description="Post a note to any platform through the endpoint",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(posting_router)
    app.include_router(tumblr_oauth_router)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
