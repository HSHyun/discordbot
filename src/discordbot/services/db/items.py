from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, List

from psycopg2.extras import Json

from ...crawl.crawl_dcinside import Post


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
    summary_title: str | None = None,
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
                INSERT INTO item_summary (item_id, model_name, summary_text, summary_title, meta)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (item_id, model_name) DO UPDATE
                SET summary_text = EXCLUDED.summary_text,
                    summary_title = EXCLUDED.summary_title,
                    meta = EXCLUDED.meta,
                    updated_at = NOW()
                """,
                (
                    item_id,
                    model_name,
                    summary,
                    summary_title or "{}",
                    json.dumps(meta_payload),
                ),
            )
    conn.commit()
