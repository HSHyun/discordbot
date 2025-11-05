#!/usr/bin/env python3
"""최신으로 요약된 DCInside 게시물을 전송하는 디스코드 봇입니다."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

import discord
from discord.ext import commands


def load_env_file(path: Path = Path(".env")) -> None:
    """간단한 .env 파일에서 환경 변수를 불러옵니다."""
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


def getenv_casefold(key: str) -> Optional[str]:
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


load_env_file()

DB_CONFIG = {
    "dbname": getenv_casefold("DB_NAME") or "discordbot",
    "user": getenv_casefold("DB_USER") or "hsh",
    "password": getenv_casefold("DB_PASSWORD") or "",
    "host": getenv_casefold("DB_HOST") or "localhost",
    "port": env_int("DB_PORT", 5432),
}

MAX_FIELD_LENGTH = 1024


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


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


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
        reserved += len(link_line) + 2  # 공백을 포함한 길이
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


def build_embed(posts: Sequence[PostSummary]) -> discord.Embed:
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


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():  # pragma: no cover - 런타임 훅
        try:
            await bot.tree.sync()
        except Exception as exc:  # pragma: no cover - 동기화 오류는 로그에만 표시
            print(f"Failed to sync commands: {exc}", file=sys.stderr)
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    @bot.tree.command(name="latest", description="요약된 게시물 상위 5개를 보여줍니다.")
    async def latest(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            posts = await asyncio.to_thread(fetch_top_posts, 5)
        except psycopg2.Error as exc:
            print(f"Database error: {exc}", file=sys.stderr)
            await interaction.followup.send(
                "데이터베이스에서 정보를 불러오지 못했습니다."
            )
            return
        if not posts:
            await interaction.followup.send("요약된 게시물이 없습니다.")
            return
        embed = build_embed(posts)
        await interaction.followup.send(embed=embed)

    return bot


def require_token() -> str:
    token = (
        getenv_casefold("DISCORD_BOT_TOKEN")
        or getenv_casefold("BOT_TOKEN")
        or os.environ.get("DISCORD_BOT_TOKEN")
    )
    if not token:
        print("DISCORD_BOT_TOKEN 환경 변수가 필요합니다.", file=sys.stderr)
        sys.exit(1)
    return token


def main() -> None:
    token = require_token()
    bot = create_bot()
    try:
        bot.run(token)
    except KeyboardInterrupt:
        print("봇을 종료합니다.")


if __name__ == "__main__":
    main()
