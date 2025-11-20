from __future__ import annotations

import asyncio
import sys
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..services.gemini import SummaryError
from .config import (
    AUTO_DIGEST_CHANNEL_ID,
    AUTO_DIGEST_HOURS,
    AUTO_DIGEST_INTERVAL_HOURS,
    DB_CONFIG,
    MAX_DIGEST_ITEMS,
)
from .digest import (
    build_best_embed,
    build_digest_embed,
    fetch_best_posts,
    fetch_digest_entries,
    summarise_digest,
)


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

        # 기본 채널 모드
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

        # 채널별 구독 처리
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
    async def on_ready():  # pragma: no cover
        try:
            await bot.tree.sync()
        except Exception as exc:  # pragma: no cover
            print(f"Failed to sync commands: {exc}", file=sys.stderr)
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
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
