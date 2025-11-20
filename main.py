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
from discord import app_commands
from discord.ext import commands, tasks

from gemini_summary import GeminiConfig, SummaryError, summarise_with_gemini


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
MAX_DIGEST_ITEMS = 300

AUTO_DIGEST_INTERVAL_HOURS = max(env_int("DIGEST_INTERVAL_HOURS", 6), 1)
AUTO_DIGEST_HOURS = max(env_int("DIGEST_HOURS", 6), 1)
AUTO_DIGEST_CHANNEL_ID = env_int("DIGEST_CHANNEL_ID", 0)


def _upsert_digest_subscription_sync(
    guild_id: Optional[int],
    channel_id: int,
    hours_window: int,
    interval_minutes: int,
) -> None:
    """digest_subscription 테이블에 (채널 기준) upsert 한다."""
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO digest_subscription (
                    guild_id,
                    channel_id,
                    hours_window,
                    interval_minutes,
                    is_active,
                    last_run_at,
                    next_run_at,
                    created_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, TRUE, NULL, NOW(), NOW(), NOW())
                ON CONFLICT (channel_id) DO UPDATE SET
                    guild_id = EXCLUDED.guild_id,
                    hours_window = EXCLUDED.hours_window,
                    interval_minutes = EXCLUDED.interval_minutes,
                    is_active = TRUE,
                    next_run_at = NOW(),
                    updated_at = NOW();
                """,
                (guild_id, channel_id, hours_window, interval_minutes),
            )
        conn.commit()


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


def fetch_digest_entries(hours: int, limit: int = MAX_DIGEST_ITEMS) -> list[dict]:
    query = """
        SELECT
            i.title,
            i.url,
            s.summary_text,
            s.model_name,
            s.updated_at,
            src.name AS source_name
        FROM item_summary s
        JOIN item i ON i.id = s.item_id
        JOIN source src ON src.id = i.source_id
        WHERE s.updated_at >= NOW() - INTERVAL %s
        ORDER BY s.updated_at DESC
        LIMIT %s
    """
    hours_interval = f"{max(hours, 1)} hours"
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (hours_interval, limit))
            return cur.fetchall()


def fetch_best_posts(hours: int, limit: int = 6) -> list[dict]:
    hours_interval = f"{max(hours, 1)} hours"
    dc_limit = 3
    reddit_limit = 3

    dc_query = """
        SELECT
            i.title,
            i.url,
            s.summary_text,
            s.summary_title,
            src.name AS source_name,
            COALESCE((i.metadata->>'views')::int, 0) AS views,
            COALESCE((i.metadata->>'recommends')::int, 0) AS recommends,
            0 AS score
        FROM item_summary s
        JOIN item i ON i.id = s.item_id
        JOIN source src ON src.id = i.source_id
        WHERE COALESCE(i.published_at, s.updated_at) >= NOW() - INTERVAL %s
          AND src.code LIKE 'dcinside%%'
        ORDER BY (
            COALESCE((i.metadata->>'recommends')::int, 0) * 10 +
            COALESCE((i.metadata->>'views')::int, 0)
        ) DESC
        LIMIT %s
    """

    reddit_query = """
        SELECT
            i.title,
            i.url,
            s.summary_text,
            s.summary_title,
            src.name AS source_name,
            COALESCE((i.metadata->>'views')::int, 0) AS views,
            COALESCE((i.metadata->>'recommends')::int, 0) AS recommends,
            COALESCE((i.metadata->>'score')::int, 0) AS score
        FROM item_summary s
        JOIN item i ON i.id = s.item_id
        JOIN source src ON src.id = i.source_id
        WHERE COALESCE(i.published_at, s.updated_at) >= NOW() - INTERVAL %s
          AND src.code LIKE 'reddit%%'
        ORDER BY COALESCE((i.metadata->>'score')::int, 0) * 10 DESC
        LIMIT %s
    """

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(dc_query, (hours_interval, dc_limit))
            dc_posts = cur.fetchall()

            cur.execute(reddit_query, (hours_interval, reddit_limit))
            reddit_posts = cur.fetchall()

    combined = list(dc_posts) + list(reddit_posts)
    if limit and limit > 0:
        return combined[:limit]
    return combined


def _build_digest_prompt(entries: Sequence[dict], hours: int) -> str:
    lines = [
        f"다음은 최근 {hours}시간 동안 수집한 게시물 요약들입니다.",
        "이 내용들을 바탕으로 현재 커뮤니티에서 가장 화제가 되고 있는 핵심 이슈 3~5가지를 선정해 브리핑해주세요.",
        "",
        "### 작성 규칙 (반드시 준수)",
        "1. **헤더 형식**: 각 이슈의 제목은 반드시 `### 1. 제목` 형식으로 작성하세요. (굵은 글씨 + 번호)",
        "2. **내용 형식**: 각 이슈당 3~4문장으로 요약하되, 구체적인 사실(모델명, 수치, 사건 등)을 포함하세요.",
        "3. **정량적 표현**: '많다', '높다', '대폭' 같은 모호한 표현 대신, **'88% 증가', '300만 토큰', '1위'** 처럼 구체적인 수치나 퍼센티지를 명확히 명시하세요.",
        "4. **문체**: '~했습니다', '~입니다' 처럼 정중하고 명확한 '해요체'나 '하십시오체'를 사용하세요. (음슴체 사용 금지)",
        "5. **구분**: 이슈 사이에는 빈 줄을 하나 넣어 가독성을 높이세요.",
        "6. **종합**: 단순 나열이 아니라, 관련된 내용끼리는 하나로 묶어서 설명하세요. (예: 'Gemini 관련 소식들')",
        "7. **언어**: 기본적으로 한국어로 적되, 전문 용어나 고유 명사는 영어로 적으세요",
        "",
        "--- 수집된 게시물 목록 ---"
    ]
    for idx, entry in enumerate(entries, start=1):
        title = entry.get("title") or "제목 없음"
        source = entry.get("source_name") or "Unknown"
        summary = (entry.get("summary_text") or "").strip()
        lines.append(f"[{idx}] {source} - {title}\n내용: {summary}")
    
    prompt = "\n".join(lines)
    return prompt[:50000]  # 넉넉하게 제한 (Gemini Flash 등 긴 컨텍스트 모델용)


def summarise_digest(entries: Sequence[dict], hours: int) -> tuple[str, str]:
    api_key = getenv_casefold("GEMINI_API_KEY2") or ""
    model_priority_raw = getenv_casefold("GEMINI_MODEL_PRIORITIES2") or ""
    model_priorities = [
        chunk.strip() for chunk in model_priority_raw.split(",") if chunk.strip()
    ] or ["gemini-2.0-flash-exp", "gemini-1.5-flash"]
    config = GeminiConfig(
        api_key=api_key,
        model_priorities=model_priorities,
        timeout_seconds=60,
        max_text_length=10000,  # 많은 양의 요약을 처리하기 위해 출력 길이 제한 상향
        cooldown_seconds=60,
    )
    prompt = _build_digest_prompt(entries, hours)
    summary, used_model = summarise_with_gemini(prompt, [], config)
    return summary, used_model


def build_digest_embed(
    hours: int,
    digest_text: Optional[str],
    digest_model: Optional[str],
) -> discord.Embed:
    description = digest_text or "요약 생성에 실패했습니다. 아래 목록을 참고하세요."
    embed = discord.Embed(
        title=f"최근 {hours}시간 일어난 일",
        description=truncate_text(description.strip(), 4000),
        colour=discord.Colour.gold(),
    )
    embed.timestamp = datetime.now(timezone.utc)
    if digest_model:
        embed.set_footer(text="")
    return embed


def build_best_embed(posts: Sequence[dict], hours: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"지난 {hours}시간 핫 토픽",
        colour=discord.Colour.brand_green(),
    )
    embed.timestamp = datetime.now(timezone.utc)
    
    for i, post in enumerate(posts, 1):
        display_title = (
            (post.get("summary_title") or "").strip()
            or (post.get("title") or "").strip()
            or "제목 없음"
        )
        url = post.get("url") or ""
        summary = (post.get("summary_text") or "요약 없음").strip()
        
        # 제목에 링크 걸기 (임베드 Value에서만 가능, Name은 불가능하므로 Value 첫줄에 처리)
        header = f"**{i}. {display_title}**"
        if url:
            header += f" [[원문]]({url})"
        

        content = f"{header}\n\n{summary}"
        embed.add_field(name="\u200b", value=truncate_text(content, 1024), inline=False)
        
    return embed


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    class AutoInfoConfirmView(discord.ui.View):
        def __init__(
            self,
            *,
            guild_id: Optional[int],
            channel_id: int,
            hours: int,
            requester_id: int,
            timeout: float = 60.0,
        ) -> None:
            super().__init__(timeout=timeout)
            self.guild_id = guild_id
            self.channel_id = channel_id
            self.hours = hours
            self.requester_id = requester_id

        async def _ensure_author(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message(
                    "이 확인 창은 명령어를 실행한 사용자만 사용할 수 있습니다.",
                    ephemeral=True,
                )
                return False
            return True

        @discord.ui.button(label="예", style=discord.ButtonStyle.success)
        async def yes_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:  # type: ignore[override]
            if not await self._ensure_author(interaction):
                return

            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

            await interaction.response.edit_message(
                content="설정을 저장하는 중입니다...", view=self
            )

            interval_minutes = max(self.hours * 60, 5)
            try:
                await asyncio.to_thread(
                    _upsert_digest_subscription_sync,
                    self.guild_id,
                    self.channel_id,
                    self.hours,
                    interval_minutes,
                )
                await interaction.edit_original_response(
                    content=(
                        f"✅ 이 채널에 **{self.hours}시간마다** 자동으로 이슈를 "
                        "보내도록 설정했습니다."
                    ),
                    view=None,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[autoinfo] 구독 저장 실패: {exc}", file=sys.stderr)
                await interaction.edit_original_response(
                    content=f"설정 저장에 실패했습니다: {exc}", view=None
                )

        @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary)
        async def no_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:  # type: ignore[override]
            if not await self._ensure_author(interaction):
                return

            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await interaction.response.edit_message(
                content="자동 이슈 등록 설정을 취소했습니다.", view=self
            )

    @tasks.loop(minutes=5)
    async def auto_digest_task() -> None:
        """digest_subscription 테이블 기준으로 각 채널에 주기적으로 다이제스트를 전송한다."""
        # 1) DB 기반 구독 설정 조회
        try:
            with psycopg2.connect(**DB_CONFIG) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT
                            id,
                            channel_id,
                            hours_window,
                            interval_minutes,
                            last_run_at,
                            next_run_at
                        FROM digest_subscription
                        WHERE is_active = TRUE
                          AND (next_run_at IS NULL OR next_run_at <= NOW())
                        """
                    )
                    subs = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            print(f"[auto-digest] 구독 설정 조회 실패: {exc}", file=sys.stderr)
            subs = []

        # 2) DB에 구독이 하나도 없고, 환경변수 기반 기본 채널이 설정된 경우 기존 단일 채널 모드로 동작
        if not subs and AUTO_DIGEST_CHANNEL_ID:
            channel = bot.get_channel(AUTO_DIGEST_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(AUTO_DIGEST_CHANNEL_ID)
                except Exception as exc:  # noqa: BLE001
                    print(f"[auto-digest] 채널 조회 실패: {exc}", file=sys.stderr)
                    return

            try:
                entries = await asyncio.to_thread(
                    fetch_digest_entries, AUTO_DIGEST_HOURS, MAX_DIGEST_ITEMS
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[auto-digest] 데이터 조회 실패: {exc}", file=sys.stderr)
                return

            if not entries:
                return

            digest_text: Optional[str] = None
            digest_model: Optional[str] = None
            try:
                digest_text, digest_model = await asyncio.to_thread(
                    summarise_digest, entries, AUTO_DIGEST_HOURS
                )
            except SummaryError as exc:
                digest_text = None
                digest_model = exc.last_model
                print(f"[auto-digest] 요약 실패: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                digest_text = None
                digest_model = None
                print(f"[auto-digest] 알 수 없는 오류: {exc}", file=sys.stderr)

            embed = build_digest_embed(AUTO_DIGEST_HOURS, digest_text, digest_model)
            try:
                await channel.send(embed=embed)
            except Exception as exc:  # noqa: BLE001
                print(f"[auto-digest] 메시지 전송 실패: {exc}", file=sys.stderr)
            return

        # 3) 채널별 구독 설정에 따라 다이제스트 전송
        for sub in subs or []:
            channel_id = sub.get("channel_id")
            hours_window = int(sub.get("hours_window") or AUTO_DIGEST_HOURS)
            interval_minutes = int(sub.get("interval_minutes") or 60)
            if not channel_id:
                continue

            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"[auto-digest] 채널 조회 실패 (channel_id={channel_id}): {exc}", file=sys.stderr)
                    continue

            try:
                entries = await asyncio.to_thread(
                    fetch_digest_entries, hours_window, MAX_DIGEST_ITEMS
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[auto-digest] 데이터 조회 실패 (channel_id={channel_id}): {exc}", file=sys.stderr)
                continue

            if not entries:
                # 그래도 next_run_at은 갱신
                try:
                    with psycopg2.connect(**DB_CONFIG) as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE digest_subscription
                                SET last_run_at = NOW(),
                                    next_run_at = NOW() + (%s || ' minutes')::interval,
                                    updated_at = NOW()
                                WHERE id = %s
                                """,
                                (interval_minutes, sub["id"]),
                            )
                            conn.commit()
                except Exception as exc:  # noqa: BLE001
                    print(f"[auto-digest] next_run_at 갱신 실패 (id={sub['id']}): {exc}", file=sys.stderr)
                continue

            digest_text: Optional[str] = None
            digest_model: Optional[str] = None
            try:
                digest_text, digest_model = await asyncio.to_thread(
                    summarise_digest, entries, hours_window
                )
            except SummaryError as exc:
                digest_text = None
                digest_model = exc.last_model
                print(f"[auto-digest] 요약 실패 (channel_id={channel_id}): {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                digest_text = None
                digest_model = None
                print(f"[auto-digest] 알 수 없는 오류 (channel_id={channel_id}): {exc}", file=sys.stderr)

            embed = build_digest_embed(hours_window, digest_text, digest_model)
            try:
                await channel.send(embed=embed)
            except Exception as exc:  # noqa: BLE001
                print(f"[auto-digest] 메시지 전송 실패 (channel_id={channel_id}): {exc}", file=sys.stderr)

            # 전송 후 next_run_at 갱신
            try:
                with psycopg2.connect(**DB_CONFIG) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE digest_subscription
                            SET last_run_at = NOW(),
                                next_run_at = NOW() + (%s || ' minutes')::interval,
                                updated_at = NOW()
                            WHERE id = %s
                            """,
                            (interval_minutes, sub["id"]),
                        )
                        conn.commit()
            except Exception as exc:  # noqa: BLE001
                print(f"[auto-digest] next_run_at 갱신 실패 (id={sub['id']}): {exc}", file=sys.stderr)

    @auto_digest_task.before_loop
    async def _auto_digest_before_loop() -> None:
        await bot.wait_until_ready()

    @bot.event
    async def on_ready():  # pragma: no cover - 런타임 훅
        try:
            await bot.tree.sync()
        except Exception as exc:  # pragma: no cover - 동기화 오류는 로그에만 표시
            print(f"Failed to sync commands: {exc}", file=sys.stderr)
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
        # 자동 다이제스트 태스크 시작
        if not auto_digest_task.is_running():
            auto_digest_task.start()

    @bot.tree.command(
        name="autoinfo",
        description="이 채널에 정해진 주기로 이슈를 보내도록 설정합니다.",
    )
    @app_commands.describe(hour="알림 주기 시간 (1-48시간 사이)")
    @app_commands.rename(hour="시간")
    async def autoinfo_command(
        interaction: discord.Interaction,
        hour: app_commands.Range[int, 1, 48],
    ) -> None:
        """현재 채널에 자동 다이제스트 구독을 설정하는 명령어."""
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "DM에서는 자동 이슈 등록를 설정할 수 없습니다.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
            await interaction.response.send_message(
                "이 타입의 채널에서는 자동 이슈 등록을 설정할 수 없습니다.",
                ephemeral=True,
            )
            return

        hours = int(hour)
        view = AutoInfoConfirmView(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            hours=hours,
            requester_id=interaction.user.id,
        )
        content = (
            f"이 채널(<#{channel.id}>)에 **{hours}시간마다** 자동 이슈 알림을 "
            "보내도록 설정할까요?"
        )
        await interaction.response.send_message(content=content, view=view, ephemeral=True)

    @bot.tree.command(name="digest", description="최근 이슈를 요약 정리합니다.")
    @app_commands.describe(hours="조회할 시간 (1-48시간 사이)")
    @app_commands.rename(hours="시간")
    async def digest_command(
        interaction: discord.Interaction,
        hours: app_commands.Range[int, 1, 48] = 6,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            entries = await asyncio.to_thread(fetch_digest_entries, hours, MAX_DIGEST_ITEMS)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"데이터를 불러오지 못했습니다: {exc}", ephemeral=True
            )
            return
        if not entries:
            await interaction.followup.send(
                f"최근 {hours}시간 이내에 요약된 게시물이 없습니다.",
                ephemeral=True,
            )
            return

        digest_text: Optional[str] = None
        digest_model: Optional[str] = None
        try:
            digest_text, digest_model = await asyncio.to_thread(
                summarise_digest, entries, hours
            )
        except SummaryError as exc:
            digest_text = None
            digest_model = exc.last_model
        except Exception:
            digest_text = None
            digest_model = None

        embed = build_digest_embed(hours, digest_text, digest_model)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="best", description="최근 핫 토픽을 보여줍니다.")
    @app_commands.describe(hours="조회할 시간 (1-48시간 사이)")
    @app_commands.rename(hours="시간")
    async def best_command(
        interaction: discord.Interaction,
        hours: app_commands.Range[int, 1, 48] = 6,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            posts = await asyncio.to_thread(fetch_best_posts, hours, 6)
        except Exception as exc:
            await interaction.followup.send(
                f"데이터를 불러오지 못했습니다: {exc}", ephemeral=True
            )
            return
            
        if not posts:
            await interaction.followup.send(
                f"최근 {hours}시간 이내의 베스트 게시물을 찾지 못했습니다.", ephemeral=True
            )
            return

        embed = build_best_embed(posts, hours)
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
