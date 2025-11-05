#!/usr/bin/env python3
"""활성화된 소스에 대해 게시글/댓글 크롤링만 수행하는 테스트 스크립트."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List

import psycopg2
from psycopg2.extras import RealDictCursor

from content_fetcher import contains_video_url, fetch_post_body
from crawl_dcinside import HEADERS as DC_HEADERS, fetch_posts as fetch_dcinside_posts
from db_utils import (
    ensure_tables,
    replace_item_assets,
    replace_item_comments,
    seed_sources_from_file,
    upsert_items,
    update_item_with_summary,
)
from store_dcinside_posts import ALLOWED_SUBJECTS, ASSET_ROOT
from store_reddit_posts import (
    _comments_for_summary,
    _normalise_reddit_comments,
    compose_post_text,
    fetch_posts_for_subreddit,
)


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


def getenv_casefold(key: str) -> str | None:
    target = key.casefold()
    for env_key, value in os.environ.items():
        if env_key.casefold() == target:
            return value
    return None


def env_int(key: str, default: int) -> int:
    raw = getenv_casefold(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


def fetch_active_sources(
    conn, platform: str | None, source_code: str | None
) -> List[dict]:
    conditions = ["is_active = TRUE"]
    params: List[object] = []
    if platform:
        conditions.append("metadata->>'platform' = %s")
        params.append(platform)
    if source_code:
        conditions.append("code = %s")
        params.append(source_code)

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT id, code, name, metadata
        FROM source
        WHERE {where_clause}
        ORDER BY id
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _download_images_if_any(image_urls, external_id: str, referer: str, asset_root: Path, headers: dict) -> tuple[list[dict], list[str]]:
    from content_fetcher import download_images  # 지연 임포트로 테스트 속도 유지

    assets = download_images(
        image_urls=image_urls,
        external_id=external_id,
        referer=referer,
        asset_root=asset_root,
        headers=headers,
    )
    paths = [asset["local_path"] for asset in assets]
    return assets, paths


def process_reddit_source(conn, source: dict) -> None:
    metadata = source.get("metadata") or {}
    subreddit = metadata.get("subreddit")
    if not subreddit:
        print(
            f"[WARN] Source {source['code']} reddit 메타데이터에 subreddit이 없습니다; 건너뜀"
        )
        return

    limit = int(metadata.get("limit") or 50)
    posts = fetch_posts_for_subreddit(subreddit)
    filtered = [
        post
        for post in posts[:limit]
        if not post.is_video and not contains_video_url(post.media_urls)
    ]
    if not filtered:
        print(f"[INFO] reddit/{subreddit} 게시물을 찾지 못했습니다.")
        return

    jobs = upsert_items(conn, source["id"], filtered)

    inserted = sum(1 for _post, _item_id, is_inserted in jobs if is_inserted)
    total_comments = 0
    asset_root = Path(metadata.get("asset_root") or f"data/reddit/{subreddit.lower()}")
    asset_root.mkdir(parents=True, exist_ok=True)

    for post, item_id, _inserted in jobs:
        raw_comments = []
        if isinstance(post.metadata, dict):
            raw_comments = post.metadata.get("comments") or []
        normalised = _normalise_reddit_comments(raw_comments)
        replace_item_comments(conn, item_id, normalised)
        total_comments += len(normalised)

        comment_lines = _comments_for_summary(normalised)
        raw_text = compose_post_text(post, comment_lines)
        image_urls = post.media_urls[:5]
        assets, image_paths = _download_images_if_any(
            image_urls,
            post.external_id,
            post.url,
            asset_root,
            REDDIT_HEADERS,
        )
        replace_item_assets(conn, item_id, assets)

        update_item_with_summary(
            conn,
            item_id,
            summary=None,
            raw_text=raw_text,
            image_count=len(image_paths),
            model_name="crawl-only",
            extra_meta={"asset_paths": image_paths},
        )
    conn.commit()

    print(
        f"[reddit] /r/{subreddit}: {len(jobs)}건 처리 (신규 {inserted}건, 댓글 {total_comments}개)"
    )


def process_dcinside_source(conn, source: dict) -> None:
    posts = [
        post for post in fetch_dcinside_posts() if post.subject in ALLOWED_SUBJECTS
    ]
    if not posts:
        print("[INFO] DCInside 게시물을 찾지 못했습니다.")
        return

    jobs = upsert_items(conn, source["id"], posts)
    inserted = sum(1 for _post, _item_id, is_inserted in jobs if is_inserted)
    total_comments = 0

    for post, item_id, _inserted in jobs:
        try:
            _raw_text, image_urls, comments = fetch_post_body(post.url, DC_HEADERS)
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[WARN] 본문 수집 실패 (no={post.external_id}): {exc}")
            continue

        if contains_video_url(image_urls):
            print(
                f"[INFO] 비디오 게시물 감지 (no={post.external_id}); 댓글 저장 없이 건너뜀"
            )
            continue

        replace_item_comments(conn, item_id, comments)
        total_comments += len(comments)

        assets, image_paths = _download_images_if_any(
            image_urls,
            post.external_id or str(item_id),
            post.url,
            ASSET_ROOT,
            DC_HEADERS,
        )
        replace_item_assets(conn, item_id, assets)

        if comment_lines:
            raw_text_with_comments = raw_text + "\n\n댓글 하이라이트:\n" + "\n".join(comment_lines)
        else:
            raw_text_with_comments = raw_text

        update_item_with_summary(
            conn,
            item_id,
            summary=None,
            raw_text=raw_text_with_comments,
            image_count=len(image_paths),
            model_name="crawl-only",
            extra_meta={"asset_paths": image_paths},
        )

    conn.commit()
    print(f"[dcinside] {len(jobs)}건 처리 (신규 {inserted}건, 댓글 {total_comments}개)")


def main() -> None:
    parser = argparse.ArgumentParser(description="활성화된 소스 크롤링만 테스트")
    parser.add_argument(
        "--platform",
        choices=["reddit", "dcinside", "all"],
        default="all",
        help="대상 플랫폼 필터",
    )
    parser.add_argument("--source", help="특정 source.code만 대상으로 실행")
    args = parser.parse_args()

    load_env_file()

    db_config = {
        "dbname": getenv_casefold("DB_NAME") or "discordbot",
        "user": getenv_casefold("DB_USER") or "hsh",
        "password": getenv_casefold("DB_PASSWORD") or "",
        "host": getenv_casefold("DB_HOST") or "localhost",
        "port": env_int("DB_PORT", 5432),
    }

    platform_filter = None if args.platform == "all" else args.platform

    with psycopg2.connect(**db_config) as conn:
        ensure_tables(conn)
        seed_sources_from_file(conn, Path("config/sources.json"))
        sources = fetch_active_sources(conn, platform_filter, args.source)
        if not sources:
            print("[INFO] 활성화된 소스를 찾지 못했습니다.")
            return

        for source in sources:
            metadata = source.get("metadata") or {}
            platform = (metadata.get("platform") or "").lower()
            if platform == "reddit":
                process_reddit_source(conn, source)
            elif platform == "dcinside":
                process_dcinside_source(conn, source)
            else:
                print(
                    f"[WARN] Source {source['code']}는 지원되지 않는 플랫폼({platform}); 건너뜀"
                )


if __name__ == "__main__":
    main()
