#!/usr/bin/env python3
"""단일 Reddit 게시물을 Codex로 요약하는 테스트 스크립트."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Optional

from codex_summary import CodexConfig, SummaryError, summarise_with_codex
from content_fetcher import contains_video_url, download_images
from crawl_reddit import (
    DEFAULT_USER_AGENT,
    RedditPost,
    fetch_reddit_post_by_url,
    fetch_reddit_posts,
)
from store_reddit_posts import (
    CODEX_DEBUG,
    CODEX_MODEL,
    CODEX_TIMEOUT_SECONDS,
    MAX_TEXT_FOR_SUMMARY,
    REDDIT_HEADERS,
    SUBREDDITS,
    compose_post_text,
)


ASSET_ROOT = Path("data/test-assets/reddit")


def select_post(
    subreddit: Optional[str],
    url: Optional[str],
    limit: int,
) -> RedditPost:
    if url:
        post = fetch_reddit_post_by_url(url, user_agent=DEFAULT_USER_AGENT)
        if post is None:
            raise RuntimeError("URL로 게시물을 찾을 수 없습니다.")
        return post

    target_subreddit = subreddit or (SUBREDDITS[0] if SUBREDDITS else None)
    if not target_subreddit:
        raise RuntimeError("대상 서브레딧을 지정할 수 없습니다.")

    posts = fetch_reddit_posts(
        target_subreddit,
        limit=limit,
        user_agent=DEFAULT_USER_AGENT,
    )

    for post in posts:
        if post.is_video:
            continue
        if contains_video_url(post.media_urls):
            continue
        return post

    raise RuntimeError("요약할 수 있는 게시물이 없습니다 (모두 비디오 게시물).")


def print_codex_io(raw_text: str, summary: str, image_paths: list[str]) -> None:
    separator = "=" * 40
    print(separator)
    print("Codex 입력 텍스트:")
    print(separator)
    print(raw_text.strip() or "(본문 없음)")
    if image_paths:
        print(separator)
        print("사용된 이미지 파일:")
        for path in image_paths:
            print(f" - {path}")
    print(separator)
    print("Codex 출력 요약:")
    print(separator)
    print(summary.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Reddit 게시물 Codex 요약 테스트")
    parser.add_argument("--subreddit", help="대상 서브레딧 (기본값: SUBREDDITS[0])")
    parser.add_argument("--url", help="요약할 게시물의 전체 URL")
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="최신 게시물 조회 시 가져올 개수 (기본값: 25)",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=5,
        help="요약에 첨부할 최대 이미지 수 (기본값: 5)",
    )
    parser.add_argument(
        "--keep-assets",
        action="store_true",
        help="요약 후 다운로드한 이미지 파일을 유지",
    )
    args = parser.parse_args()

    post = select_post(args.subreddit, args.url, args.limit)

    print(f"대상 게시물: [r/{post.subreddit}] {post.title}")
    print(f"URL: {post.url}")
    print(f"작성자: u/{post.author} | 업보트: {post.score} | 댓글: {post.num_comments}")

    raw_text = compose_post_text(post)

    asset_root = ASSET_ROOT / post.subreddit
    asset_root.mkdir(parents=True, exist_ok=True)
    external_id = post.external_id.lstrip("t3_") if post.external_id else "manual"
    asset_dir = asset_root / external_id
    if asset_dir.exists() and not args.keep_assets:
        shutil.rmtree(asset_dir)

    limited_urls = post.media_urls[: args.max_images] if args.max_images > 0 else []
    assets = download_images(
        image_urls=limited_urls,
        external_id=external_id,
        referer=post.url,
        asset_root=asset_root,
        headers=REDDIT_HEADERS,
    )
    image_paths = [asset["local_path"] for asset in assets]

    codex_config = CodexConfig(
        model=CODEX_MODEL,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
        max_text_length=MAX_TEXT_FOR_SUMMARY,
        debug=CODEX_DEBUG,
    )

    try:
        summary = summarise_with_codex(raw_text, image_paths, codex_config)
    except SummaryError as exc:
        raise RuntimeError(f"Codex 요약이 실패했습니다: {exc}") from exc

    print_codex_io(raw_text, summary, image_paths)

    comments = post.metadata.get("comments") if isinstance(post.metadata, dict) else None
    if comments:
        print("\n상위 댓글 (최대 5개):")
        for index, comment in enumerate(comments[:5], start=1):
            author = comment.get("author") or "unknown"
            body = (comment.get("body") or "").strip()
            score = comment.get("score")
            print(f"[{index}] u/{author} (score={score}): {body}")

    if not args.keep_assets and asset_dir.exists():
        shutil.rmtree(asset_dir)


if __name__ == "__main__":
    main()
