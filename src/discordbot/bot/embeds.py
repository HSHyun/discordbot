from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

import discord

from .config import DB_CONFIG, MAX_FIELD_LENGTH


@dataclass(slots=True)
class PostSummary:
    title: str
    url: str
    summary: str
    published_at: Optional[datetime]
    first_seen_at: Optional[datetime]
    date_display: Optional[str]
    subject: Optional[str]
    author: Optional[str]
    comment_count: Optional[int]
    views: Optional[int]
    recommends: Optional[int]


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    try:
        return int(trimmed)
    except ValueError:
        return None


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def fetch_top_posts(limit: int = 5) -> list[PostSummary]:
    query = """
        WITH latest_summary AS (
            SELECT DISTINCT ON (item_id)
                item_id,
                summary_text,
                created_at
            FROM item_summary
            ORDER BY item_id, created_at DESC, id DESC
        )
        SELECT
            i.id,
            i.title,
            i.url,
            ls.summary_text,
            i.published_at,
            i.first_seen_at,
            i.author,
            i.metadata->>'date_display' AS date_display,
            i.metadata->>'subject' AS subject,
            i.metadata->>'comment_count' AS comment_count,
            i.metadata->>'views' AS views,
            i.metadata->>'recommends' AS recommends
        FROM item i
        JOIN latest_summary ls ON ls.item_id = i.id
        ORDER BY COALESCE(i.published_at, i.first_seen_at) DESC, i.id DESC
        LIMIT %s
        """
    posts: list[PostSummary] = []
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (limit,))
            for row in cur.fetchall():
                posts.append(
                    PostSummary(
                        title=row.get("title") or "제목 없음",
                        url=row.get("url") or "",
                        summary=(row.get("summary_text") or "").strip(),
                        published_at=row.get("published_at"),
                        first_seen_at=row.get("first_seen_at"),
                        date_display=row.get("date_display"),
                        subject=row.get("subject"),
                        author=row.get("author"),
                        comment_count=_to_int(row.get("comment_count")),
                        views=_to_int(row.get("views")),
                        recommends=_to_int(row.get("recommends")),
                    )
                )
    return posts


def format_timestamp(post: PostSummary) -> Optional[str]:
    ts = post.published_at or post.first_seen_at
    if ts is None:
        if post.date_display:
            return post.date_display
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime("%Y-%m-%d %H:%M")


def build_field_value(post: PostSummary) -> str:
    summary = post.summary or "(요약 없음)"
    link_line = f"[원문 보기]({post.url})" if post.url else ""
    meta_parts: list[str] = []
    if post.subject:
        meta_parts.append(post.subject)
    posted = format_timestamp(post)
    if posted:
        meta_parts.append(f"게시 {posted}")
    if post.recommends is not None:
        meta_parts.append(f"추천 {post.recommends}")
    if post.views is not None:
        meta_parts.append(f"조회 {post.views}")
    if post.comment_count is not None:
        meta_parts.append(f"댓글 {post.comment_count}")
    meta_line = " • ".join(meta_parts)

    pieces = [summary]
    if link_line:
        pieces.append(link_line)
    if meta_line:
        pieces.append(meta_line)
    field = "\n\n".join(pieces[:2]) if len(pieces) >= 2 else summary
    if len(pieces) > 2:
        field = field + "\n" + meta_line

    if len(field) <= MAX_FIELD_LENGTH:
        return field

    reserved = 0
    if link_line:
        reserved += len(link_line) + 2
    if meta_line:
        reserved += len(meta_line) + 1
    summary_limit = max(10, MAX_FIELD_LENGTH - reserved)
    trimmed_summary = truncate_text(summary, summary_limit)

    rebuilt: list[str] = [trimmed_summary]
    if link_line:
        rebuilt.append(link_line)
    if meta_line:
        rebuilt.append(meta_line)
    final = "\n\n".join(rebuilt[:2]) if len(rebuilt) >= 2 else trimmed_summary
    if len(rebuilt) > 2:
        final = final + "\n" + rebuilt[-1]
    return truncate_text(final, MAX_FIELD_LENGTH)


def build_recent_embed(posts: Sequence[PostSummary]) -> discord.Embed:
    embed = discord.Embed(
        title="최신 요약 상위 5개",
        description="요약이 수집된 최근 게시물을 보여줍니다.",
        colour=discord.Colour.blurple(),
    )
    embed.timestamp = datetime.now(timezone.utc)
    for index, post in enumerate(posts, start=1):
        name = truncate_text(f"{index}. {post.title}", 256)
        embed.add_field(name=name, value=build_field_value(post), inline=False)
    embed.set_footer(text="출처: DCInside 특이점 추천")
    return embed
