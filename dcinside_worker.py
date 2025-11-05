"""DCInside 게시물 요약 RabbitMQ 워커."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

import psycopg2
import requests

from codex_summary import CodexConfig, SummaryError, summarise_with_codex
from content_fetcher import contains_video_url, download_images, fetch_post_body
from crawl_dcinside import HEADERS
from db_utils import (
    delete_item,
    replace_item_assets,
    replace_item_comments,
    update_item_with_summary,
)
from worker_common import (
    MessageHandlingError,
    MessageHandlingResult,
    RabbitMQClient,
    env_flag,
    env_int,
    getenv_casefold,
    load_env_file,
    serve,
)


load_env_file()

logger = logging.getLogger(__name__)

DB_CONFIG = {
    "dbname": getenv_casefold("DB_NAME") or "discordbot",
    "user": getenv_casefold("DB_USER") or "hsh",
    "password": getenv_casefold("DB_PASSWORD") or "",
    "host": getenv_casefold("DB_HOST") or "localhost",
    "port": env_int("DB_PORT", 5432),
}

QUEUE_NAME = getenv_casefold("DCINSIDE_QUEUE") or "dcinside_items"
ASSET_ROOT = Path("data/assets")
ASSET_ROOT.mkdir(parents=True, exist_ok=True)

CODEX_MODEL = getenv_casefold("CODEX_MODEL") or "gpt-5-codex"
CODEX_TIMEOUT = env_int("CODEX_TIMEOUT", 300)
MAX_TEXT_LENGTH = env_int("CODEX_MAX_TEXT", 4000)
CODEX_DEBUG = env_flag("CODEX_DEBUG")


def _format_comments_for_summary(comments: List[dict]) -> List[str]:
    lines: List[str] = []
    if not comments:
        return lines

    id_to_author = {
        str(comment.get("external_id")): (comment.get("author") or "unknown")
        for comment in comments
        if comment.get("external_id") is not None
    }

    for comment in comments:
        author = comment.get("author") or "unknown"
        content = (comment.get("content") or "").strip()
        if not content:
            continue

        metadata = comment.get("metadata") or {}
        depth_raw = metadata.get("depth")
        try:
            depth = int(depth_raw) if depth_raw is not None else 0
        except (TypeError, ValueError):
            depth = 0

        parent_external = comment.get("parent_external_id")
        parent_author = None
        if parent_external is not None:
            parent_author = id_to_author.get(str(parent_external))

        indent = "  " * max(depth, 0)
        label = "[원댓글]" if depth <= 0 else (
            f"[대댓글 → {parent_author}]" if parent_author else "[대댓글]"
        )
        lines.append(f"{indent}{label} {author}: {content}")

    return lines


def _fetch_item(conn, item_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, external_id, url
            FROM item
            WHERE id = %s
            """,
            (item_id,),
        )
        row = cur.fetchone()
    if not row:
        raise MessageHandlingError(f"Item {item_id} not found", requeue=False)
    return {"id": row[0], "external_id": row[1], "url": row[2]}


def _build_codex_config() -> CodexConfig:
    return CodexConfig(
        model=CODEX_MODEL,
        timeout_seconds=CODEX_TIMEOUT,
        max_text_length=MAX_TEXT_LENGTH,
        debug=CODEX_DEBUG,
    )


def process_message(body: bytes) -> MessageHandlingResult:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MessageHandlingError(f"Invalid message payload: {exc}", requeue=False) from exc

    item_id = payload.get("item_id")
    if not isinstance(item_id, int):
        raise MessageHandlingError("Message missing valid item_id", requeue=False)

    with psycopg2.connect(**DB_CONFIG) as conn:
        item = _fetch_item(conn, item_id)
        try:
            body_text, image_urls, comments = fetch_post_body(item["url"], HEADERS)
        except requests.RequestException as exc:
            update_item_with_summary(
                conn,
                item_id,
                summary=None,
                raw_text="",
                image_count=0,
                model_name=CODEX_MODEL,
                last_error=f"Detail fetch failed: {exc}",
            )
            return MessageHandlingResult(True, f"Detail fetch failed for item {item_id}")

        if contains_video_url(image_urls):
            delete_item(conn, item_id)
            return MessageHandlingResult(True, f"Skipped video post {item_id}")

        replace_item_comments(conn, item_id, comments)

        comment_lines = _format_comments_for_summary(comments)
        summary_input = body_text or ""
        if comment_lines:
            summary_input = (
                summary_input
                + "\n\n댓글 전체 목록 (원댓글/대댓글 구조):\n"
                + "\n".join(comment_lines)
            )

        assets = download_images(
            image_urls=image_urls,
            external_id=item["external_id"] or str(item_id),
            referer=item["url"],
            asset_root=ASSET_ROOT,
            headers=HEADERS,
        )
        replace_item_assets(conn, item_id, assets)

        image_paths = [asset["local_path"] for asset in assets]
        codex_config = _build_codex_config()

        last_error = None
        try:
            summary = summarise_with_codex(summary_input, image_paths, codex_config)
        except SummaryError as exc:
            last_error = str(exc)
            summary = summary_input[: codex_config.max_text_length] if summary_input else None

        update_item_with_summary(
            conn,
            item_id,
            summary,
            summary_input,
            image_count=len(image_paths),
            model_name=codex_config.model,
            last_error=last_error,
        )

    return MessageHandlingResult(True, f"Processed item {item_id}")


def main() -> None:
    client = RabbitMQClient(QUEUE_NAME)
    serve("dcinside-worker", client, process_message)


if __name__ == "__main__":
    main()
