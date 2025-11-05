#!/usr/bin/env python3
"""DCInside 게시물을 가져와 저장하고 Codex CLI로 요약합니다."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import psycopg2
import requests

from crawl_dcinside import HEADERS, Post, TARGET_URL, fetch_posts
from codex_summary import CodexConfig, SummaryError, summarise_with_codex
from content_fetcher import contains_video_url, download_images, fetch_post_body
from db_utils import (
    SourceConfig,
    ensure_tables,
    get_or_create_source,
    replace_item_assets,
    replace_item_comments,
    delete_item,
    seed_sources_from_file,
    upsert_items,
    update_item_with_summary,
)


def load_env_file(path: Path = Path(".env")) -> None:
    """.env 파일의 키와 값을 읽어 환경 변수로 설정합니다."""
    if not path.exists():
        return

    try:
        with path.open("r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                cleaned_value = value.strip()
                if (
                    len(cleaned_value) >= 2
                    and cleaned_value[0] == cleaned_value[-1]
                    and cleaned_value[0] in {'"', "'"}
                ):
                    cleaned_value = cleaned_value[1:-1]
                os.environ[key] = cleaned_value
    except OSError:
        pass


load_env_file()


def getenv_casefold(key: str) -> str | None:
    """환경 변수 키의 대소문자와 상관없이 값을 반환합니다."""
    target = key.casefold()
    for env_key, value in os.environ.items():
        if env_key.casefold() == target:
            return value
    return None


def env_flag(key: str, default: bool = False) -> bool:
    """대소문자를 구분하지 않고 환경 변수 값을 불리언으로 해석합니다."""
    value = getenv_casefold(key)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "off", "no"}


def env_int(key: str, default: int) -> int:
    """정수형 환경 변수 값을 읽고 실패하면 기본값을 반환합니다."""
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

ASSET_ROOT = Path("data/assets")
ASSET_ROOT.mkdir(parents=True, exist_ok=True)

CODEX_DEBUG = env_flag("CODEX_DEBUG")
CODEX_MODEL = getenv_casefold("CODEX_MODEL") or "gpt-5-codex"
CODEX_TIMEOUT_SECONDS = env_int("CODEX_TIMEOUT", 300)
MAX_TEXT_FOR_SUMMARY = 4000


def _comment_lines_for_summary(comments: List[dict]) -> List[str]:
    """댓글 전체를 구조 정보와 함께 요약 입력에 포함할 수 있도록 문자열로 만든다."""
    if not comments:
        return []

    id_to_author = {
        str(comment.get("external_id")): (comment.get("author") or "unknown")
        for comment in comments
        if comment.get("external_id") is not None
    }

    lines: List[str] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
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

        parent_external = comment.get("parent_external_id")
        parent_author = None
        if parent_external is not None:
            parent_author = id_to_author.get(str(parent_external))

        indent = "  " * max(depth_level, 0)
        if depth_level <= 0:
            label = "[원댓글]"
        else:
            label = f"[대댓글 → {parent_author}]" if parent_author else "[대댓글]"

        line = f"{indent}{label} {author}: {content}"
        lines.append(line)

    return lines


def process_details(
    conn,
    jobs,
    codex_config: CodexConfig,
    asset_root: Path = ASSET_ROOT,
) -> None:
    """상세 페이지를 수집하고 에셋을 관리하며 콘텐츠를 요약합니다."""
    for post, item_id, _inserted in jobs:
        try:
            body_text, image_urls, comments = fetch_post_body(post.url, HEADERS)
        except requests.RequestException as exc:
            body_text, image_urls, comments = "", [], []
            last_error = f"Detail fetch failed: {exc}"
            update_item_with_summary(
                conn,
                item_id,
                summary=None,
                raw_text=body_text,
                image_count=0,
                model_name=codex_config.model,
                last_error=last_error,
            )
            continue

        if contains_video_url(image_urls):
            print(f"Skipping video post {post.url}; deleting item {item_id}.")
            delete_item(conn, item_id)
            continue

        replace_item_comments(conn, item_id, comments)
        summary_input = body_text
        comment_lines = _comment_lines_for_summary(comments)
        if comment_lines:
            summary_input = (
                summary_input
                + "\n\n댓글 전체 목록 (원댓글/대댓글 구조):\n"
                + "\n".join(comment_lines)
            )

        assets = download_images(
            image_urls=image_urls,
            external_id=post.external_id or str(item_id),
            referer=post.url,
            asset_root=asset_root,
            headers=HEADERS,
        )
        replace_item_assets(conn, item_id, assets)

        image_paths = [asset["local_path"] for asset in assets]
        last_error = None
        summary = None
        try:
            summary = summarise_with_codex(summary_input, image_paths, codex_config)
        except SummaryError as exc:
            last_error = str(exc)
            summary = (
                summary_input[: codex_config.max_text_length] if summary_input else None
            )

        update_item_with_summary(
            conn,
            item_id,
            summary,
            body_text,
            image_count=len(image_paths),
            model_name=codex_config.model,
            last_error=last_error,
        )


def main() -> None:
    posts = [post for post in fetch_posts() if post.subject in ALLOWED_SUBJECTS]
    if not posts:
        print("No posts matched allowed subjects; aborting.")
        return

    codex_config = CodexConfig(
        model=CODEX_MODEL,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
        max_text_length=MAX_TEXT_FOR_SUMMARY,
        debug=CODEX_DEBUG,
    )

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
        process_details(conn, jobs, codex_config)

    print(f"Processed {len(jobs)} posts (details fetched & summarised).")


if __name__ == "__main__":
    main()
