from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def load_env_file(path: Path = Path(".env")) -> None:
    """간단한 .env 파일에서 환경 변수를 불러옵니다."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                cleaned = value.strip()
                if (
                    len(cleaned) >= 2
                    and cleaned[0] == cleaned[-1]
                    and cleaned[0] in {'"', "'"}
                ):
                    cleaned = cleaned[1:-1]
                os.environ[key] = cleaned
    except OSError:
        pass


def getenv_casefold(key: str) -> Optional[str]:
    target = key.casefold()
    for env_key, value in os.environ.items():
        if env_key.casefold() == target:
            return value
    return None


def env_int(key: str, default: int) -> int:
    raw = getenv_casefold(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


load_env_file()

DB_CONFIG = {
    "dbname": getenv_casefold("DB_NAME") or "discordbot",
    "user": getenv_casefold("DB_USER") or "hsh",
    "password": getenv_casefold("DB_PASSWORD") or "",
    "host": getenv_casefold("DB_HOST") or "localhost",
    "port": env_int("DB_PORT", 5432),
}

MAX_FIELD_LENGTH = 1024
MAX_DIGEST_ITEMS = 300

AUTO_DIGEST_INTERVAL_HOURS = max(env_int("DIGEST_INTERVAL_HOURS", 6), 1)
AUTO_DIGEST_HOURS = max(env_int("DIGEST_HOURS", 6), 1)
AUTO_DIGEST_CHANNEL_ID = env_int("DIGEST_CHANNEL_ID", 0)


def require_token() -> str:
    token = (
        getenv_casefold("DISCORD_BOT_TOKEN")
        or getenv_casefold("BOT_TOKEN")
        or os.environ.get("DISCORD_BOT_TOKEN")
    )
    if not token:
        print("DISCORD_BOT_TOKEN 환경 변수가 필요합니다.", file=sys.stderr)
        sys.exit(1)
    return token
