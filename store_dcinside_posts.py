#!/usr/bin/env python3
"""DCInside 게시물을 upsert하고 RabbitMQ 작업을 발행한다."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pika
import psycopg2

from crawl_dcinside import Post, TARGET_URL, fetch_posts
from db_utils import (
    SourceConfig,
    ensure_tables,
    get_or_create_source,
    seed_sources_from_file,
    upsert_items,
)


def load_env_file(path: Path = Path(".env")) -> None:
    """.env 파일을 읽어 환경 변수에 주입한다."""
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
    "dbname": "discordbot",
    "user": "hsh",
    "password": "",
    "host": "localhost",
    "port": 5432,
}

SOURCE_CONFIG = SourceConfig(
    code="dcinside_thesingularity_recommend",
    name="DCInside 특이점 추천",
    url_pattern=(
        "https://gall.dcinside.com/mgallery/board/view/?id=thesingularity&no={external_id}"
        "&exception_mode=recommend&page={page}"
    ),
    parser="dcinside_recommend_v1",
    fetch_interval_minutes=60,
    metadata={
        "board_id": "thesingularity",
        "exception_mode": "recommend",
        "target_url": TARGET_URL,
    },
)

ALLOWED_SUBJECTS = {
    "일반",
    "정보/뉴스",
    "🏆베스트",
    "사용후기",
    "AI활용",
    "자료실",
    "역노화",
    "토의",
    "대회",
}

MAX_FETCH_POSTS = env_int("DCINSIDE_MAX_POSTS", 0)
MIN_POST_AGE_HOURS = env_int("DCINSIDE_MIN_POST_AGE_HOURS", 0)
DCINSIDE_QUEUE = getenv_casefold("DCINSIDE_QUEUE") or "dcinside_items"


def _parse_post_datetime(post: Post) -> datetime | None:
    if not post.date_iso:
        return None
    try:
        parsed = datetime.strptime(post.date_iso, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


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


def _filter_posts(posts: List[Post]) -> List[Post]:
    filtered = [post for post in posts if post.subject in ALLOWED_SUBJECTS]
    if MIN_POST_AGE_HOURS > 0:
        threshold = datetime.now(timezone.utc) - timedelta(hours=MIN_POST_AGE_HOURS)
        aged: List[Post] = []
        for post in filtered:
            published_at = _parse_post_datetime(post)
            if published_at and published_at <= threshold:
                aged.append(post)
        filtered = aged
    if MAX_FETCH_POSTS > 0:
        filtered = filtered[:MAX_FETCH_POSTS]
    return filtered


def main() -> None:
    posts = _filter_posts(list(fetch_posts()))
    if not posts:
        print("No posts matched filters; aborting.")
        return

    with psycopg2.connect(**DB_CONFIG) as conn:
        ensure_tables(conn)
        seed_sources_from_file(conn, Path("config/sources.json"))
        source, created = get_or_create_source(conn, SOURCE_CONFIG)
        if created:
            print(
                "Created source configuration with is_active=FALSE. "
                "Update the record to enable crawling."
            )
        if not source.get("is_active"):
            print(f"Source '{source.get('code')}' is inactive; skipping crawl.")
            return

        jobs = upsert_items(conn, source["id"], posts)

    inserted_ids = [item_id for _post, item_id, inserted in jobs if inserted]
    _publish_item_ids(DCINSIDE_QUEUE, inserted_ids)
    print(f"Queued {len(inserted_ids)} DCInside posts for processing.")


if __name__ == "__main__":
    main()
