from __future__ import annotations

from typing import Sequence, Optional
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

import discord

from ..services.gemini import GeminiConfig, SummaryError, summarise_with_gemini
from .config import DB_CONFIG, MAX_DIGEST_ITEMS
from .config import getenv_casefold, env_int

from .embeds import truncate_text  # noqa: E402


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
        "4. **문체**: '~했습니다', '~입니다' 처럼 정중하고 명확한 '해요체'나 '십시오체'를 사용하세요. (음슴체 사용 금지)",
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
    return prompt[:50000]


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
        max_text_length=10000,
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

        header = f"**{i}. {display_title}**"
        if url:
            header += f" [[원문]]({url})"

        content = f"{header}\n\n{summary}"
        embed.add_field(name="\u200b", value=truncate_text(content, 1024), inline=False)

    return embed
