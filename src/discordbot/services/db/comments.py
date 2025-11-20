from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, List


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
