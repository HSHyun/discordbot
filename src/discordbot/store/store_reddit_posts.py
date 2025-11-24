#!/usr/bin/env python3
"""Reddit 서브레딧 게시물을 upsert하고 RabbitMQ에 작업을 발행합니다."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pika
import psycopg2

if __package__ is None or __package__ == "":
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[2]))

from discordbot.crawl.crawl_reddit import DEFAULT_USER_AGENT, RedditPost, fetch_reddit_posts
from discordbot.services.content_fetcher import contains_video_url
from discordbot.services.db import SourceConfig, get_or_create_source, seed_sources_from_file, upsert_items

SUBREDDITS = ["OpenAI", "singularity", "ClaudeAI"]

REDDIT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json",
}


def load_env_file(path: Path = Path(".env")) -> None:
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


load_env_file()


def getenv_casefold(key: str) -> str | None:
    target = key.casefold()
    for env_key, value in os.environ.items():
        if env_key.casefold() == target:
            return value
    return None


def env_int(key: str, default: int) -> int:
    value = getenv_casefold(key)
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


DB_CONFIG = {
    "dbname": getenv_casefold("DB_NAME") or "discordbot",
    "user": getenv_casefold("DB_USER") or "hsh",
    "password": getenv_casefold("DB_PASSWORD") or "",
    "host": getenv_casefold("DB_HOST") or "localhost",
    "port": env_int("DB_PORT", 5432),
}

MAX_POSTS_PER_SUBREDDIT = env_int("REDDIT_MAX_POSTS", 0)
MIN_POST_AGE_HOURS = env_int("REDDIT_MIN_POST_AGE_HOURS", 0)
MAX_POST_AGE_HOURS = env_int("REDDIT_MAX_POST_AGE_HOURS", 6)
REDDIT_QUEUE = getenv_casefold("REDDIT_QUEUE") or "reddit_items"


def build_source_config(subreddit: str) -> SourceConfig:
    slug = subreddit.lower()
    return SourceConfig(
        code=f"reddit_{slug}_new",
        name=f"Reddit /r/{subreddit}",
        url_pattern=f"https://www.reddit.com/r/{subreddit}/comments/{{external_id}}",
        parser="reddit_new_v1",
        fetch_interval_minutes=60,
        metadata={
            "platform": "reddit",
            "subreddit": subreddit,
            "target_url": f"https://www.reddit.com/r/{subreddit}/new/",
            "limit": 50,
            "asset_root": f"data/reddit/{slug}",
            "use_api": False,
        },
    )


def _publish_item_ids(queue_name: str, item_ids: List[int]) -> None:
    if not queue_name or not item_ids:
        return
    url = getenv_casefold("RABBITMQ_URL") or "amqp://guest:guest@localhost:5672/%2F"
    params = pika.URLParameters(url)
    connection: pika.BlockingConnection | None = None
    channel: pika.channel.Channel | None = None
    try:
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        channel.queue_declare(queue=queue_name, durable=True)
        for item_id in item_ids:
            payload = json.dumps({"item_id": item_id}).encode("utf-8")
            channel.basic_publish(exchange="", routing_key=queue_name, body=payload)
    finally:
        if channel and channel.is_open:
            channel.close()
        if connection and connection.is_open:
            connection.close()


def fetch_posts_for_subreddit(subreddit: str) -> List[RedditPost]:
    limit = MAX_POSTS_PER_SUBREDDIT if MAX_POSTS_PER_SUBREDDIT > 0 else 50
    max_age = MAX_POST_AGE_HOURS if MAX_POST_AGE_HOURS > 0 else None
    return fetch_reddit_posts(
        subreddit,
        limit=limit,
        user_agent=DEFAULT_USER_AGENT,
        max_age_hours=max_age,
    )


def _filter_posts(posts: List[RedditPost]) -> tuple[List[RedditPost], int]:
    filtered = []
    now = datetime.now(timezone.utc)
    min_threshold = (
        now - timedelta(hours=MIN_POST_AGE_HOURS) if MIN_POST_AGE_HOURS > 0 else None
    )
    max_threshold = (
        now - timedelta(hours=MAX_POST_AGE_HOURS) if MAX_POST_AGE_HOURS > 0 else None
    )

    for post in posts:
        if post.is_video or contains_video_url(post.media_urls):
            continue
        published_at = post.created_utc
        if min_threshold and published_at > min_threshold:
            continue
        if max_threshold and published_at < max_threshold:
            continue
        filtered.append(post)
    return filtered, len(filtered)


def _log_crawl_run(
    conn,
    source_name: str,
    queued_count: int,
    fetched_count: int,
    filtered_count: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_run_log (
                source,
                queued_count,
                fetched_count,
                filtered_count
            ) VALUES (%s, %s, %s, %s)
            """,
            (source_name, queued_count, fetched_count, filtered_count),
        )


def _normalise_reddit_comments(raw_comments: List[dict]) -> List[dict]:
    normalised: List[dict] = []
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue
        comment_id = comment.get("name") or comment.get("id")
        if not comment_id:
            continue
        if not comment_id.startswith("t1_"):
            comment_id = f"t1_{comment_id}"
        parent_id = comment.get("parent_id") or ""
        parent_external = (
            parent_id
            if isinstance(parent_id, str) and parent_id.startswith("t1_")
            else None
        )
        body = (comment.get("body") or "").strip()
        metadata = (
            comment.get("metadata") if isinstance(comment.get("metadata"), dict) else {}
        )
        if metadata:
            metadata = {k: v for k, v in metadata.items() if v is not None}
        if "score" not in metadata and comment.get("score") is not None:
            metadata["score"] = comment.get("score")
        normalised.append(
            {
                "external_id": comment_id,
                "author": comment.get("author") or "unknown",
                "content": body,
                "created_at": comment.get("created_utc"),
                "is_deleted": bool(comment.get("is_deleted"))
                or body.lower() in {"[deleted]", "[removed]"},
                "metadata": {
                    **metadata,
                    "depth": comment.get("depth"),
                },
                "parent_external_id": parent_external,
                "score": comment.get("score"),
            }
        )
    return normalised


def _comments_for_summary(comments: List[dict]) -> List[str]:
    if not comments:
        return []

    id_to_author = {
        comment.get("external_id"): (comment.get("author") or "unknown")
        for comment in comments
        if comment.get("external_id")
    }

    display_lines: List[str] = []
    for comment in comments:
        author = comment.get("author") or "unknown"
        content = (comment.get("content") or "").strip()
        if not content:
            continue

        metadata = comment.get("metadata") or {}
        depth = metadata.get("depth")
        try:
            depth_level = int(depth) if depth is not None else 0
        except (TypeError, ValueError):
            depth_level = 0

        score = comment.get("score")
        score_text = f" (+{score})" if isinstance(score, (int, float)) else ""

        parent_external = comment.get("parent_external_id")
        parent_author = id_to_author.get(parent_external)

        indent = "  " * max(depth_level, 0)
        if depth_level <= 0:
            label = "[원댓글]"
        else:
            label = f"[대댓글 → {parent_author}]" if parent_author else "[대댓글]"

        line = f"{indent}{label} {author}{score_text}: {content}"
        display_lines.append(line)

    return display_lines


def compose_post_text(post: RedditPost, comment_lines: List[str] | None = None) -> str:
    lines: List[str] = [post.title]
    if post.selftext.strip():
        lines.append(post.selftext.strip())
    else:
        lines.append("(텍스트 본문 없음 — 링크/미디어 게시물입니다.)")
    meta_line = (
        f"작성자: u/{post.author} | 업보트: {post.score} | 댓글: {post.num_comments}"
    )
    lines.append(meta_line)
    if post.url:
        lines.append(f"원문: {post.url}")
    if comment_lines:
        lines.append(
            "\n".join(["댓글 전체 목록 (원댓글/대댓글 구조):"] + comment_lines)
        )
    return "\n\n".join(lines)


def main() -> None:
    with psycopg2.connect(**DB_CONFIG) as conn:
        seed_sources_from_file(conn, Path("config/sources.json"))
        total_queued = 0
        for subreddit in SUBREDDITS:
            source_config = build_source_config(subreddit)
            source, created = get_or_create_source(conn, source_config)
            if created:
                print(
                    f"Created source for r/{subreddit} with is_active=FALSE. "
                    "Activate the source to enable crawling."
                )

            if not source.get("is_active"):
                print(f"Source '{source.get('code')}' inactive; skipping r/{subreddit}.")
                continue

            posts = fetch_posts_for_subreddit(subreddit)
            if not posts:
                print(f"No posts fetched for r/{subreddit}; skipping.")
                continue

            filtered_posts, filtered_count = _filter_posts(posts)
            if not filtered_posts:
                print(f"No eligible posts for r/{subreddit}; skipping.")
                continue

            jobs = upsert_items(conn, source["id"], filtered_posts)
            item_ids = [item_id for _post, item_id, _inserted in jobs]
            _log_crawl_run(
                conn,
                source.get("name") or source_config.name,
                len(item_ids),
                len(posts),
                filtered_count,
            )
            _publish_item_ids(REDDIT_QUEUE, item_ids)
            total_queued += len(item_ids)
            print(f"Queued {len(item_ids)} posts for r/{subreddit}.")

    print(f"Queued {total_queued} Reddit posts (details will be processed by workers).")


if __name__ == "__main__":
    main()
