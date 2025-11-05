"""Reddit 게시물 요약 RabbitMQ 워커."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import psycopg2

from codex_summary import CodexConfig, SummaryError, summarise_with_codex
from content_fetcher import contains_video_url, download_images
from crawl_reddit import RedditPost, fetch_reddit_post_by_url
from db_utils import (
    replace_item_assets,
    replace_item_comments,
    update_item_with_summary,
)
from store_reddit_posts import (
    REDDIT_HEADERS,
    _comments_for_summary,
    _normalise_reddit_comments,
    compose_post_text,
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

QUEUE_NAME = getenv_casefold("REDDIT_QUEUE") or "reddit_items"
ASSET_ROOT = Path("data/reddit")
ASSET_ROOT.mkdir(parents=True, exist_ok=True)

CODEX_MODEL = getenv_casefold("CODEX_MODEL") or "gpt-5-codex"
CODEX_TIMEOUT = env_int("CODEX_TIMEOUT", 180)
MAX_TEXT_LENGTH = env_int("CODEX_MAX_TEXT", 4000)
CODEX_DEBUG = env_flag("CODEX_DEBUG")



def _build_codex_config() -> CodexConfig:
    return CodexConfig(
        model=CODEX_MODEL,
        timeout_seconds=CODEX_TIMEOUT,
        max_text_length=MAX_TEXT_LENGTH,
        debug=CODEX_DEBUG,
    )


def _build_asset_root(post: RedditPost) -> Path:
    root = ASSET_ROOT / (post.subreddit.lower() or "misc")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fetch_item_url(conn, item_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT url FROM item WHERE id = %s", (item_id,))
        row = cur.fetchone()
    if not row:
        raise MessageHandlingError(f"Item {item_id} not found", requeue=False)
    return row[0]


def process_message(body: bytes) -> MessageHandlingResult:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MessageHandlingError(f"Invalid message payload: {exc}", requeue=False) from exc

    item_id = payload.get("item_id")
    if not isinstance(item_id, int):
        raise MessageHandlingError("Message missing valid item_id", requeue=False)

    with psycopg2.connect(**DB_CONFIG) as conn:
        item_url = _fetch_item_url(conn, item_id)
        try:
            post = fetch_reddit_post_by_url(item_url)
        except Exception as exc:  # noqa: BLE001
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

        if post is None:
            update_item_with_summary(
                conn,
                item_id,
                summary=None,
                raw_text="",
                image_count=0,
                model_name=CODEX_MODEL,
                last_error="Reddit post not found",
            )
            return MessageHandlingResult(True, f"Missing post for item {item_id}")

        if post.is_video or contains_video_url(post.media_urls):
            update_item_with_summary(
                conn,
                item_id,
                summary=None,
                raw_text="",
                image_count=0,
                model_name=CODEX_MODEL,
                last_error="Skipped video post",
            )
            return MessageHandlingResult(True, f"Skipped video post {item_id}")

        normalised_comments = _normalise_reddit_comments(post.metadata.get("comments") or [])
        replace_item_comments(conn, item_id, normalised_comments)

        comment_lines = _comments_for_summary(normalised_comments)
        summary_input = compose_post_text(post, comment_lines)

        asset_root = _build_asset_root(post)
        assets = download_images(
            image_urls=post.media_urls,
            external_id=post.external_id,
            referer=post.url,
            asset_root=asset_root,
            headers=REDDIT_HEADERS,
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
    serve("reddit-worker", client, process_message)


if __name__ == "__main__":
    main()
