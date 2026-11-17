from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    enabled_platforms: str = "devto,bluesky,tumblr,gist,jsonplaceholder"

    request_timeout: float = Field(default=15, gt=0)
    connect_timeout: float = Field(default=5, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    # Optional per-platform overrides, e.g. "devto=3,tumblr=0".
    platform_max_retries: str = ""

    devto_base_url: str = ""
    devto_api_key: str = ""

    bluesky_service_url: str = ""
    bluesky_web_url: str = ""
    bluesky_handle: str = ""
    bluesky_app_password: str = ""

    tumblr_api_base_url: str = ""
    tumblr_authorize_url: str = ""
    tumblr_token_url: str = ""
    tumblr_blog_identifier: str = ""
    tumblr_consumer_key: str = ""
    tumblr_secret_key: str = ""
    tumblr_access_token: str = ""
    tumblr_refresh_token: str = ""
    tumblr_redirect_uri: str = ""
    tumblr_token_store_path: str = ".secrets/tumblr_tokens.json"

    github_api_base_url: str = ""
    github_api_version: str = ""
    github_gist_token: str = ""
    github_gist_public: bool = True

    jsonplaceholder_base_url: str = ""
    jsonplaceholder_user_id: int = Field(default=1, ge=1)

    @field_validator("*")
    @classmethod
    def check_url_settings(cls, value: object, info) -> object:
        is_url_setting = info.field_name.endswith(("_url", "_uri"))
        if is_url_setting and value and not str(value).startswith(("http://", "https://")):
            raise ValueError(f"{info.field_name.upper()} must start with http:// or https://")
        return value

    @field_validator("platform_max_retries")
    @classmethod
    def check_retry_overrides(cls, value: str) -> str:
        _parse_retry_overrides(value)
        return value

    @property
    def enabled_platform_keys(self) -> list[str]:
        keys = [key.strip().lower() for key in self.enabled_platforms.split(",")]
        return list(dict.fromkeys(key for key in keys if key))

    def max_retries_for(self, platform_key: str) -> int:
        return _parse_retry_overrides(self.platform_max_retries).get(platform_key, self.max_retries)

    def secret_values(self) -> list[str]:
        secrets = [
            self.devto_api_key,
            self.bluesky_app_password,
            self.tumblr_secret_key,
            self.tumblr_access_token,
            self.tumblr_refresh_token,
            self.github_gist_token,
        ]
        return [secret for secret in secrets if secret]


def _parse_retry_overrides(raw: str) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        key, separator, count = entry.partition("=")
        if not separator or not count.strip().isdigit():
            raise ValueError(
                f"PLATFORM_MAX_RETRIES entry '{entry}' must look like 'platform=number'"
            )
        overrides[key.strip().lower()] = int(count)
    return overrides


@lru_cache
def get_settings() -> Settings:
    return Settings()
