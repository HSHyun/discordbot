"""DCInside 게시물 요약 RabbitMQ 워커."""

from __future__ import annotations

import json
import time
import logging
from pathlib import Path
from typing import List

import psycopg2
import requests

from gemini_summary import (
    GeminiConfig,
    SummaryError,
    summarise_with_gemini_with_title,
)
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

DEFAULT_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


def _parse_model_list(raw: str | None) -> List[str]:
    if not raw:
        return DEFAULT_GEMINI_MODELS.copy()
    models = [chunk.strip() for chunk in raw.split(",")]
    parsed = [model for model in models if model]
    return parsed or DEFAULT_GEMINI_MODELS.copy()


GEMINI_API_KEY = getenv_casefold("GEMINI_API_KEY") or ""
GEMINI_MODEL_PRIORITIES = _parse_model_list(getenv_casefold("GEMINI_MODEL_PRIORITIES"))
GEMINI_TIMEOUT = env_int("GEMINI_TIMEOUT", 300)
MAX_TEXT_LENGTH = env_int("GEMINI_MAX_TEXT", 4000)
GEMINI_DEBUG = env_flag("GEMINI_DEBUG")
GEMINI_COOLDOWN = env_int("GEMINI_MODEL_COOLDOWN", 600)
THROTTLE_SECONDS = env_int("WORKER_THROTTLE_SECONDS", 0)

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


def _primary_model() -> str:
    return GEMINI_MODEL_PRIORITIES[0] if GEMINI_MODEL_PRIORITIES else DEFAULT_GEMINI_MODELS[0]


def _build_gemini_config() -> GeminiConfig:
    return GeminiConfig(
        api_key=GEMINI_API_KEY,
        model_priorities=GEMINI_MODEL_PRIORITIES,
        timeout_seconds=GEMINI_TIMEOUT,
        max_text_length=MAX_TEXT_LENGTH,
        debug=GEMINI_DEBUG,
        cooldown_seconds=GEMINI_COOLDOWN,
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
                model_name=_primary_model(),
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
        gemini_config = _build_gemini_config()

        last_error = None
        model_used = _primary_model()
        summary_title = None
        try:
            summary, summary_title, model_used = summarise_with_gemini_with_title(
                summary_input, image_paths, gemini_config
            )
        except SummaryError as exc:
            last_error = str(exc)
            fallback_limit = gemini_config.max_text_length
            if summary_input:
                summary = summary_input[:fallback_limit]
                first_line = summary_input.splitlines()[0].strip()
                summary_title = first_line[:80] if first_line else "요약"
            else:
                summary = None
                summary_title = None
            model_used = getattr(exc, "last_model", None) or model_used

        update_item_with_summary(
            conn,
            item_id,
            summary,
            summary_input,
            image_count=len(image_paths),
            model_name=model_used,
            summary_title=summary_title,
            last_error=last_error,
        )
        if THROTTLE_SECONDS > 0:
            time.sleep(THROTTLE_SECONDS)
    return MessageHandlingResult(True, f"Processed item {item_id}")


def main() -> None:
    client = RabbitMQClient(QUEUE_NAME)
    serve("dcinside-worker", client, process_message)


if __name__ == "__main__":
    main()
