"""Configuration, loaded from .env. No network, no defaults that phone home."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to run. Constructed once at import."""

    noop_db_path: Path | None
    host: str
    port: int
    display_tz: str
    schema_map_path: Path
    app_db_path: Path
    web_dir: Path = field(default=REPO_ROOT / "web")

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.display_tz)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @property
    def noop_db_exists(self) -> bool:
        return self.noop_db_path is not None and self.noop_db_path.is_file()


def load_settings() -> Settings:
    _load_env()
    raw_db = os.getenv("NOOP_DB_PATH", "").strip()
    db_path = Path(raw_db).expanduser() if raw_db else None

    raw_app_db = os.getenv("APP_DB_PATH", "").strip()
    app_db = Path(raw_app_db).expanduser() if raw_app_db else REPO_ROOT / "data" / "dashboard.sqlite3"

    raw_map = os.getenv("SCHEMA_MAP_PATH", "").strip()
    schema_map = Path(raw_map).expanduser() if raw_map else REPO_ROOT / "schema_map.json"

    return Settings(
        noop_db_path=db_path,
        # 0.0.0.0 so the PWA on the phone can reach it over the LAN. This binds to
        # every interface — keep it on a network you trust; there is no auth layer.
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8765")),
        # NOOP buckets days by UTC (see SCHEMA_NOTES.md 1.4). This tz is used only
        # to decide what "today" means when you ask for it, and for display.
        display_tz=os.getenv("DISPLAY_TZ", "UTC"),
        schema_map_path=schema_map,
        app_db_path=app_db,
    )


settings = load_settings()
