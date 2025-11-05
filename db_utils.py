"""데이터베이스 관련 유틸리티 함수 모음."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Tuple

from psycopg2.extras import Json, RealDictCursor

from crawl_dcinside import Post


@dataclass(frozen=True)
class SourceConfig:
    """소스 테이블에 기록할 설정 값."""

    code: str
    name: str
    url_pattern: str
    parser: str
    fetch_interval_minutes: int
    metadata: dict


def ensure_tables(conn) -> None:
    """필요한 테이블과 인덱스를 생성하고 기본값을 정비한다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS source (
                id SERIAL PRIMARY KEY,
                code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(200) NOT NULL,
                url_pattern TEXT NOT NULL,
                parser VARCHAR(100) NOT NULL,
                fetch_interval_minutes INT DEFAULT 60,
                is_active BOOLEAN NOT NULL DEFAULT FALSE,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS item (
                id BIGSERIAL PRIMARY KEY,
                source_id INT NOT NULL REFERENCES source(id),
                external_id VARCHAR(200) NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                author TEXT,
                content TEXT,
                published_at TIMESTAMPTZ,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                metadata JSONB DEFAULT '{}'::jsonb
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS item_asset (
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
                asset_type VARCHAR(50) NOT NULL,
                url TEXT,
                local_path TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_item_source_external
            ON item (source_id, external_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_item_asset_item_id
            ON item_asset (item_id);
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS item_summary (
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                meta JSONB NOT NULL DEFAULT '{}'::jsonb
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_item_summary_item_id
            ON item_summary (item_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_item_summary_created_at
            ON item_summary (created_at);
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS comment (
                id BIGSERIAL PRIMARY KEY,
                item_id BIGINT NOT NULL REFERENCES item(id) ON DELETE CASCADE,
                external_id TEXT NOT NULL,
                author TEXT,
                content TEXT,
                created_at TIMESTAMPTZ,
                is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                parent_id BIGINT REFERENCES comment(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_comment_item_external
            ON comment (item_id, external_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_comment_item_id
            ON comment (item_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_comment_parent_id
            ON comment (parent_id);
            """
        )
        cur.execute(
            """
            ALTER TABLE source
            ALTER COLUMN is_active SET DEFAULT FALSE
            """
        )
    conn.commit()


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
    """JSON 파일에서 소스 설정을 읽어 `source` 테이블을 채운다."""
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


def _parse_published_at(post: Post):
    if not post.date_iso:
        return None
    try:
        return datetime.strptime(post.date_iso, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _build_metadata(post: Post) -> dict:
    comment_text = post.comments.strip()
    if comment_text.startswith("[") and comment_text.endswith("]"):
        comment_text = comment_text[1:-1]
    try:
        comment_count = int(comment_text) if comment_text else None
    except ValueError:
        comment_count = None

    metadata = {
        "display_number": post.number,
        "subject": post.subject,
        "comment_count": comment_count,
        "views": post.views,
        "recommends": post.recommends,
        "date_display": post.date_display,
        "date_iso": post.date_iso,
    }
    extra = getattr(post, "metadata", None)
    if isinstance(extra, dict):
        metadata.update({k: v for k, v in extra.items() if v is not None})
    return metadata


def _parse_comment_created_at(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def replace_item_comments(conn, item_id: int, comments: Iterable[dict]) -> None:
    """기존 댓글을 모두 제거하고 새롭게 저장한다."""
    comment_list = list(comments)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM comment WHERE item_id = %s", (item_id,))
        if not comment_list:
            return

        inserted_ids: dict[str, int] = {}
        pending: List[tuple[dict, str | None]] = []

        for raw in comment_list:
            external_id = str(raw.get("external_id") or raw.get("id") or "").strip()
            if not external_id:
                continue
            author = raw.get("author") or raw.get("user") or ""
            content = raw.get("content") or raw.get("body") or ""
            metadata = raw.get("metadata") or {}
            is_deleted = bool(raw.get("is_deleted"))
            parent_external = raw.get("parent_external_id") or raw.get("parent_id")
            created_at = _parse_comment_created_at(raw.get("created_at") or raw.get("created_utc"))

            cur.execute(
                """
                INSERT INTO comment (
                    item_id,
                    external_id,
                    author,
                    content,
                    created_at,
                    is_deleted,
                    metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    item_id,
                    external_id,
                    author,
                    content,
                    created_at,
                    is_deleted,
                    json.dumps(metadata),
                ),
            )
            inserted_id = cur.fetchone()[0]
            inserted_ids[external_id] = inserted_id
            if isinstance(parent_external, str) and parent_external.strip():
                pending.append((external_id, parent_external.strip()))

        for external_id, parent_external in pending:
            parent_id = inserted_ids.get(parent_external)
            if not parent_id:
                continue
            cur.execute(
                """
                UPDATE comment
                SET parent_id = %s
                WHERE item_id = %s AND external_id = %s
                """,
                (parent_id, item_id, external_id),
            )
    # Commit is handled by the caller's transaction scope.


def upsert_items(conn, source_id: int, posts: Iterable[Post]):
    """게시물 정보를 upsert하고 처리 결과를 반환한다."""
    results = []
    with conn.cursor() as cur:
        for post in posts:
            metadata = _build_metadata(post)
            published_at = _parse_published_at(post)
            cur.execute(
                """
                INSERT INTO item (
                    source_id, external_id, url, title, author, content,
                    published_at, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, external_id) DO UPDATE SET
                    url = EXCLUDED.url,
                    title = EXCLUDED.title,
                    author = EXCLUDED.author,
                    content = COALESCE(item.content, EXCLUDED.content),
                    published_at = COALESCE(EXCLUDED.published_at, item.published_at),
                    metadata = item.metadata || EXCLUDED.metadata
                RETURNING id, (xmax = 0) AS inserted;
                """,
                (
                    source_id,
                    post.external_id or post.number,
                    post.url,
                    post.title,
                    post.writer,
                    None,
                    published_at,
                    Json(metadata),
                ),
            )
            item_id, inserted_flag = cur.fetchone()
            results.append((post, item_id, bool(inserted_flag)))
    conn.commit()
    return results


def replace_item_assets(conn, item_id: int, assets: List[dict]) -> None:
    """아이템에 연결된 에셋을 새 목록으로 교체한다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM item_asset WHERE item_id = %s", (item_id,))
        if not assets:
            return
        insert_values = [
            (
                item_id,
                asset["asset_type"],
                asset.get("url"),
                asset.get("local_path"),
                Json(asset.get("metadata", {})),
            )
            for asset in assets
        ]
        args_str = b",".join(
            cur.mogrify("(%s,%s,%s,%s,%s)", vals) for vals in insert_values
        )
        cur.execute(
            b"INSERT INTO item_asset (item_id, asset_type, url, local_path, metadata) VALUES "
            + args_str
        )
    conn.commit()


def delete_item(conn, item_id: int) -> None:
    """지정한 아이템과 연관 에셋을 삭제한다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM item WHERE id = %s", (item_id,))
    conn.commit()


def update_item_with_summary(
    conn,
    item_id: int,
    summary: str | None,
    raw_text: str,
    image_count: int,
    model_name: str,
    last_error: str | None = None,
    extra_meta: dict | None = None,
) -> None:
    """요약 정보를 item_summary에 기록하고 item 메타데이터를 갱신한다."""
    metadata_patch = {
        "raw_text": raw_text,
        "image_count": image_count,
        "summary_generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if last_error:
        metadata_patch["summary_error"] = last_error
    else:
        metadata_patch["summary_error"] = None

    meta_payload = {
        "image_count": image_count,
        "raw_text_length": len(raw_text),
        "last_error": last_error,
    }
    if extra_meta:
        meta_payload.update(extra_meta)

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE item
            SET content = %s,
                metadata = metadata || %s::jsonb
            WHERE id = %s
            """,
            (
                raw_text,
                json.dumps(metadata_patch),
                item_id,
            ),
        )
        if summary:
            cur.execute(
                """
                INSERT INTO item_summary (item_id, model_name, summary_text, meta)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    item_id,
                    model_name,
                    summary,
                    json.dumps(meta_payload),
                ),
            )
    conn.commit()
