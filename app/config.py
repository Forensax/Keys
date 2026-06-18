from __future__ import annotations

import os
import secrets
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


class Settings:
    app_name: str = os.getenv("APP_NAME", "中转站管理")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'keys.db'}")
    session_secret: str = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    proxy_test_url: str = os.getenv("PROXY_TEST_URL", "https://api.ipify.org?format=json")


settings = Settings()
