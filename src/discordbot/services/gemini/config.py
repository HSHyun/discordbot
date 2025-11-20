from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Sequence


class SummaryError(Exception):
    """Gemini 요약 처리 실패."""

    def __init__(self, message: str, *, last_model: str | None = None) -> None:
        super().__init__(message)
        self.last_model = last_model


@dataclass(frozen=True)
class GeminiConfig:
    """Gemini 호출에 필요한 설정."""

    api_key: str
    model_priorities: Sequence[str]
    timeout_seconds: int
    max_text_length: int
    debug: bool = False
    api_endpoint: str = "https://generativelanguage.googleapis.com"
    cooldown_seconds: int = 600
    image_limit: int = 8
    generation_config: Dict[str, object] = field(
        default_factory=lambda: {
            "temperature": 0.4,
            "topP": 0.95,
            "topK": 40,
            "maxOutputTokens": 8192,
        }
    )


_MODEL_COOLDOWNS: Dict[str, float] = {}


def set_cooldown(model: str, seconds: int) -> None:
    _MODEL_COOLDOWNS[model] = time.time() + max(seconds, 0)


def cooldown_until(model: str) -> float:
    return _MODEL_COOLDOWNS.get(model, 0.0)


def clear_cooldown(model: str) -> None:
    _MODEL_COOLDOWNS.pop(model, None)
