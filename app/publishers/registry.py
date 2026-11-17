import httpx

from app.config import Settings
from app.publishers.base import Publisher
from app.publishers.bluesky import BlueskyPublisher
from app.publishers.devto import DevtoPublisher
from app.publishers.gist import GistPublisher
from app.publishers.jsonplaceholder import JsonPlaceholderPublisher
from app.publishers.tumblr import TumblrPublisher

# Adding a platform means writing its adapter and listing it here.
PUBLISHER_CLASSES: dict[str, type[Publisher]] = {
    publisher_class.key: publisher_class
    for publisher_class in (
        DevtoPublisher,
        BlueskyPublisher,
        TumblrPublisher,
        GistPublisher,
        JsonPlaceholderPublisher,
    )
}


def build_publishers(settings: Settings, http_client: httpx.AsyncClient) -> list[Publisher]:
    unknown = [key for key in settings.enabled_platform_keys if key not in PUBLISHER_CLASSES]
    if unknown:
        raise ValueError(
            f"ENABLED_PLATFORMS contains unknown platforms: {', '.join(unknown)}. "
            f"Available platforms: {', '.join(PUBLISHER_CLASSES)}"
        )
    return [
        PUBLISHER_CLASSES[key](settings, http_client) for key in settings.enabled_platform_keys
    ]
