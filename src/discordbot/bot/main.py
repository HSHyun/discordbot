#!/usr/bin/env python3
"""최신 요약을 Discord 채널에 전송하는 봇 실행 엔트리."""

from __future__ import annotations

from pathlib import Path
import sys

if __package__ is None or __package__ == "":
    # 스크립트를 직접 실행할 때 패키지 경로를 추가한다.
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from discordbot.bot.commands import create_bot
from discordbot.bot.config import require_token


def main() -> None:
    token = require_token()
    bot = create_bot()
    bot.run(token)


if __name__ == "__main__":
    main()
