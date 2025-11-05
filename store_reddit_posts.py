#!/usr/bin/env python3
"""Reddit 서브레딧 게시물을 수집해 DB에 저장하고 Codex로 요약합니다."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List

import psycopg2

from crawl_reddit import DEFAULT_USER_AGENT, RedditPost, fetch_reddit_posts
from codex_summary import CodexConfig, SummaryError, summarise_with_codex
from content_fetcher import contains_video_url, download_images
from db_utils import (
    SourceConfig,
    ensure_tables,
    get_or_create_source,
    replace_item_assets,
    replace_item_comments,
    seed_sources_from_file,
    upsert_items,
    update_item_with_summary,
)

SUBREDDITS = ["OpenAI", "singularity", "ClaudeAI"]

REDDIT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json",
}

REDDIT_ASSET_ROOT = Path("data/reddit")
REDDIT_ASSET_ROOT.mkdir(parents=True, exist_ok=True)


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
    "dbname": getenv_casefold("DB_NAME") or "discordbot",
    "user": getenv_casefold("DB_USER") or "hsh",
    "password": getenv_casefold("DB_PASSWORD") or "",
    "host": getenv_casefold("DB_HOST") or "localhost",
    "port": env_int("DB_PORT", 5432),
}

CODEX_DEBUG = env_flag("CODEX_DEBUG")
CODEX_MODEL = getenv_casefold("CODEX_MODEL") or "gpt-5-codex"
CODEX_TIMEOUT_SECONDS = env_int("CODEX_TIMEOUT", 180)
MAX_TEXT_FOR_SUMMARY = env_int("CODEX_MAX_TEXT", 4000)


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
    """댓글과 대댓글 관계를 유지한 설명용 문자열을 생성합니다."""
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
            if parent_author:
                label = f"[대댓글 → {parent_author}]"
            else:
                label = "[대댓글]"

        line = f"{indent}{label} {author}{score_text}: {content}"
        display_lines.append(line)

    return display_lines


def process_jobs(
    conn,
    jobs,
    subreddit: str,
    codex_config: CodexConfig,
) -> None:
    asset_root = REDDIT_ASSET_ROOT / subreddit.lower()
    asset_root.mkdir(parents=True, exist_ok=True)

    for post, item_id, _inserted in jobs:
        assert isinstance(post, RedditPost)
        raw_comments: List[dict] = []
        if isinstance(post.metadata, dict):
            raw_comments = post.metadata.get("comments") or []
        normalised_comments = _normalise_reddit_comments(raw_comments)
        replace_item_comments(conn, item_id, normalised_comments)

        comment_lines = _comments_for_summary(normalised_comments)
        raw_text = compose_post_text(post, comment_lines)
        image_urls = post.media_urls
        assets = download_images(
            image_urls=image_urls,
            external_id=post.external_id,
            referer=post.url,
            asset_root=asset_root,
            headers=REDDIT_HEADERS,
        )
        replace_item_assets(conn, item_id, assets)

        image_paths = [asset["local_path"] for asset in assets]
        last_error = None
        summary = None
        try:
            summary = summarise_with_codex(raw_text, image_paths, codex_config)
        except SummaryError as exc:
            last_error = str(exc)
            summary = raw_text[: codex_config.max_text_length] if raw_text else None

        if summary:
            update_item_with_summary(
                conn,
                item_id,
                summary,
                raw_text,
                image_count=len(image_paths),
                model_name=codex_config.model,
                last_error=last_error,
            )


def fetch_posts_for_subreddit(subreddit: str) -> List[RedditPost]:
    limit = 50
    return fetch_reddit_posts(
        subreddit,
        limit=limit,
        user_agent=DEFAULT_USER_AGENT,
    )


def main() -> None:
    codex_config = CodexConfig(
        model=CODEX_MODEL,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
        max_text_length=MAX_TEXT_FOR_SUMMARY,
        debug=CODEX_DEBUG,
    )

    with psycopg2.connect(**DB_CONFIG) as conn:
        ensure_tables(conn)
        seed_sources_from_file(conn, Path("config/sources.json"))
        total_jobs = 0
        for subreddit in SUBREDDITS:
            posts = fetch_posts_for_subreddit(subreddit)
            if not posts:
                print(f"No posts fetched for r/{subreddit}; skipping.")
                continue

            source_config = build_source_config(subreddit)
            source, created = get_or_create_source(conn, source_config)
            if created:
                print(
                    f"Created source for r/{subreddit} with is_active=FALSE. "
                    "Activate the source to enable crawling."
                )

            if not source.get("is_active"):
                print(
                    f"Source '{source.get('code')}' inactive; skipping r/{subreddit}."
                )
                continue

            filtered_posts = [
                post
                for post in posts
                if not post.is_video and not contains_video_url(post.media_urls)
            ]
            if not filtered_posts:
                print(f"No eligible posts (non-video) for r/{subreddit}; skipping.")
                continue

            jobs = upsert_items(conn, source["id"], filtered_posts)
            if not jobs:
                print(f"No upserted items for r/{subreddit}.")
                continue

            process_jobs(conn, jobs, subreddit, codex_config)
            total_jobs += len(jobs)

    print(f"Processed {total_jobs} Reddit posts (details & summaries).")


if __name__ == "__main__":
    main()
