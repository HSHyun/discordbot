from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from psycopg2.extras import RealDictCursor


@dataclass(frozen=True)
class SourceConfig:
    """소스 테이블에 기록할 설정 값."""

    code: str
    name: str
    url_pattern: str
    parser: str
    fetch_interval_minutes: int
    metadata: dict


def _source_config_from_dict(payload: dict) -> SourceConfig:
    required_keys = ["code", "name", "url_pattern", "parser"]
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise ValueError(f"Source config missing keys: {', '.join(missing)}")

    return SourceConfig(
        code=str(payload["code"]),
        name=str(payload["name"]),
        url_pattern=str(payload["url_pattern"]),
        parser=str(payload["parser"]),
        fetch_interval_minutes=int(payload.get("fetch_interval_minutes") or 60),
        metadata=dict(payload.get("metadata") or {}),
    )


def seed_sources_from_file(conn, path: Path) -> tuple[int, int]:
    """JSON 파일에서 소스 설정을 읽어 source 테이블을 채운다."""
    if not path.exists():
        return 0, 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to load source seed file: {path}") from exc

    if not isinstance(data, list):
        raise ValueError("Source seed file must contain a list of source configs.")

    created_count = 0
    total = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        total += 1
        config = _source_config_from_dict(entry)
        _, created = get_or_create_source(conn, config)
        if created:
            created_count += 1

    return created_count, total


def get_or_create_source(conn, config: SourceConfig) -> tuple[dict, bool]:
    """소스 설정 행과 생성 여부를 반환한다."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, code, name, url_pattern, parser,
                   fetch_interval_minutes, is_active, metadata
            FROM source
            WHERE code = %s
            """,
            (config.code,),
        )
        existing = cur.fetchone()
        if existing:
            return dict(existing), False

        cur.execute(
            """
            INSERT INTO source (
                code, name, url_pattern, parser,
                fetch_interval_minutes, metadata, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (code) DO NOTHING
            RETURNING id, code, name, url_pattern, parser,
                      fetch_interval_minutes, is_active, metadata
            """,
            (
                config.code,
                config.name,
                config.url_pattern,
                config.parser,
                config.fetch_interval_minutes,
                json.dumps(config.metadata),
            ),
        )
        inserted = cur.fetchone()
        if inserted:
            conn.commit()
            return dict(inserted), True

        cur.execute(
            """
            SELECT id, code, name, url_pattern, parser,
                   fetch_interval_minutes, is_active, metadata
            FROM source
            WHERE code = %s
            """,
            (config.code,),
        )
        refetched = cur.fetchone()
        if refetched is None:
            raise RuntimeError("Failed to locate source configuration after insert attempt.")
        return dict(refetched), False
