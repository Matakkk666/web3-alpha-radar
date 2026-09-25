"""Application settings loaded from the .env file in the project root."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "radar.db"

load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    bot_token: str
    admin_id: int
    gemini_api_key: str | None = None
    proxy_url: str | None = None
    auth_token: str | None = None
    twitter_list_url: str | None = None
    log_level: str = "INFO"
    timezone: str = "UTC"

    database_url: str = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"

    @field_validator("gemini_api_key", "proxy_url", "auth_token", "twitter_list_url", mode="before")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _load_settings() -> Settings:
    return Settings(
        bot_token=_first_env("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
        admin_id=_first_env("TELEGRAM_ADMIN_ID", "ADMIN_ID", default="0"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        proxy_url=os.getenv("PROXY_URL"),
        auth_token=_first_env("X_AUTH_TOKEN", "AUTH_TOKEN") or None,
        twitter_list_url=_first_env("X_LIST_URL", "TWITTER_LIST_URL") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        timezone=os.getenv("TIMEZONE") or "UTC",
    )


settings = _load_settings()
