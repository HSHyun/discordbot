#!/usr/bin/env python3
"""단일 DCInside 게시물을 Codex로 요약하는 테스트 스크립트."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from codex_summary import CodexConfig, SummaryError, summarise_with_codex
from content_fetcher import contains_video_url, download_images, fetch_post_body
from crawl_dcinside import HEADERS, Post, fetch_posts
from store_dcinside_posts import (
    ALLOWED_SUBJECTS,
    CODEX_DEBUG,
    CODEX_MODEL,
    CODEX_TIMEOUT_SECONDS,
    MAX_TEXT_FOR_SUMMARY,
)


ASSET_ROOT = Path("data/test-assets/dcinside")


def _build_manual_post(url: str) -> Post:
    parsed = urlparse(url)
    external_id = parse_qs(parsed.query).get("no", ["manual"])[0]
    return Post(
        external_id=external_id,
        number=external_id,
        subject="(사용자 지정)",
        title="사용자 지정 게시물",
        url=url,
        comments="",
        writer="",
        date_display="",
        date_iso="",
        views="",
        recommends="",
    )


def select_post(target_url: Optional[str]) -> Post:
    if target_url:
        for post in fetch_posts():
            if (
                post.url == target_url
                or post.external_id
                and post.external_id in target_url
            ):
                return post
        return _build_manual_post(target_url)

    for post in fetch_posts():
        if not ALLOWED_SUBJECTS or post.subject in ALLOWED_SUBJECTS:
            return post
    raise RuntimeError("가져올 수 있는 게시물이 없습니다.")


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
    parser = argparse.ArgumentParser(description="DCInside 게시물 Codex 요약 테스트")
    parser.add_argument("--url", help="직접 지정할 게시물 URL")
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

    post = select_post(args.url)
    if not post.url:
        raise RuntimeError("게시물 URL을 확인할 수 없습니다.")

    print(f"대상 게시물: {post.title or '(제목 없음)'}")
    print(f"URL: {post.url}")

    try:
        raw_text, image_urls, comments = fetch_post_body(post.url, HEADERS)
    except Exception as exc:
        raise RuntimeError(f"본문을 가져오지 못했습니다: {exc}") from exc

    if contains_video_url(image_urls):
        raise RuntimeError("비디오가 포함된 게시물은 Codex 요약을 건너뜁니다.")

    comment_lines = []
    for comment in comments[:5]:
        if not isinstance(comment, dict):
            continue
        body = (comment.get("content") or "").strip()
        if not body:
            continue
        author = comment.get("author") or "unknown"
        comment_lines.append(f"{author}: {body}")
    if comment_lines:
        raw_text = raw_text + "\n\n댓글 하이라이트:\n" + "\n".join(comment_lines)

    asset_root = ASSET_ROOT
    asset_root.mkdir(parents=True, exist_ok=True)
    external_id = post.external_id or "manual"
    asset_dir = asset_root / external_id
    if asset_dir.exists() and not args.keep_assets:
        shutil.rmtree(asset_dir)

    limited_urls = image_urls[: args.max_images] if args.max_images > 0 else []
    assets = download_images(
        image_urls=limited_urls,
        external_id=external_id,
        referer=post.url,
        asset_root=asset_root,
        headers=HEADERS,
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

    if comment_lines:
        print("\n참고한 댓글:")
        for line in comment_lines:
            print(f" - {line}")

    if not args.keep_assets and asset_dir.exists():
        shutil.rmtree(asset_dir)


if __name__ == "__main__":
    main()
