"""Gemini API를 이용해 게시물을 요약한다."""

from __future__ import annotations

import base64
import json
import mimetypes
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

import requests


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
            "maxOutputTokens": 512,
        }
    )


# 모델별 쿨다운 만료 시간을 저장한다.
_MODEL_COOLDOWNS: Dict[str, float] = {}


class _ModelQuotaError(SummaryError):
    """모델 사용량 한도 초과 또는 일시적인 제한을 나타낸다."""


SYSTEM_PROMPT = (
    "당신은 DCInside나 Reddit 게시물을 한국어로 요약하는 전문가입니다. 출력은 반드시 자연스럽고"
    " 문어체에 가까운 한국어 문장으로 작성하며 불릿 포인트나 영어 문장은 사용하지 않습니다."
    " 답변은 3문장 이내로 유지하고 링크는 제외합니다. 이미지에서 확인한 핵심 내용이 있다면 본문"
    " 맥락에 자연스럽게 녹여 설명합니다. 고유명사는 원문 표기를 유지하세요."
)


def summarise_with_gemini(
    text: str,
    image_paths: Sequence[str],
    config: GeminiConfig,
) -> Tuple[str, str]:
    """Gemini 모델 우선순위를 적용해 요약을 생성하고 (요약문, 사용 모델)을 반환한다."""

    if not config.api_key:
        raise SummaryError("Missing GEMINI_API_KEY.")

    text_to_use = (text or "").strip()
    if not text_to_use:
        if image_paths:
            text_to_use = "(본문 텍스트 없음 — 이미지를 기반으로 요약해 주세요.)"
        else:
            raise SummaryError("No text or images available for summarisation.")

    if len(text_to_use) > config.max_text_length:
        text_to_use = text_to_use[: config.max_text_length] + "\n..."

    user_prompt = (
        "아래는 게시물 원문과 참고 이미지입니다. 중요 내용을 3문장 이내로 요약해 주세요.\n\n"
        f"{text_to_use}"
    )

    available_models = [model.strip() for model in config.model_priorities if model.strip()]
    if not available_models:
        raise SummaryError("No Gemini models configured.")

    image_parts = list(_build_image_parts(image_paths, config.image_limit))

    errors: List[str] = []
    now = time.time()
    last_attempted_model: str | None = None
    for model in available_models:
        cooldown_until = _MODEL_COOLDOWNS.get(model)
        if cooldown_until and cooldown_until > now:
            continue

        try:
            last_attempted_model = model
            summary = _invoke_gemini(
                model=model,
                user_prompt=user_prompt,
                image_parts=image_parts,
                config=config,
            )
        except _ModelQuotaError as exc:
            print(
                f"[GEMINI WARN] Model {model} quota error: {exc}", file=sys.stderr
            )
            _MODEL_COOLDOWNS[model] = time.time() + max(config.cooldown_seconds, 30)
            errors.append(str(exc))
            continue
        except SummaryError as exc:
            print(
                f"[GEMINI WARN] Model {model} failed: {exc}", file=sys.stderr
            )
            errors.append(str(exc))
            continue
        else:
            _MODEL_COOLDOWNS.pop(model, None)
            return summary, model

    if errors:
        raise SummaryError(errors[-1], last_model=last_attempted_model)
    raise SummaryError(
        "All Gemini models were skipped due to cooldown or configuration.",
        last_model=last_attempted_model,
    )


def _build_image_parts(image_paths: Sequence[str], limit: int) -> Iterable[dict]:
    count = 0
    for path in image_paths:
        if count >= max(limit, 0):
            break
        if not path:
            continue
        try:
            with open(path, "rb") as image_file:
                data = image_file.read()
        except OSError:
            continue
        if not data:
            continue
        mime_type, _ = mimetypes.guess_type(path)
        if not mime_type:
            mime_type = "application/octet-stream"
        encoded = base64.b64encode(data).decode("ascii")
        yield {"inline_data": {"mimeType": mime_type, "data": encoded}}
        count += 1


def _invoke_gemini(
    *,
    model: str,
    user_prompt: str,
    image_parts: Sequence[dict],
    config: GeminiConfig,
) -> str:
    base_url = config.api_endpoint.rstrip("/")
    url = f"{base_url}/v1beta/models/{model}:generateContent"

    contents = [
        {
            "role": "user",
            "parts": [{"text": user_prompt}, *image_parts],
        }
    ]

    payload = {
        "systemInstruction": {"role": "system", "parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": config.generation_config,
    }

    if config.debug:
        debug_payload = _redact_image_data(payload)
        debug_lines = [
            f"[GEMINI DEBUG] Model: {model}",
            "[GEMINI DEBUG] Payload (images omitted):",
            json.dumps(debug_payload, ensure_ascii=False),
        ]
        print("\n".join(debug_lines), file=sys.stderr)

    try:
        response = requests.post(
            url,
            params={"key": config.api_key},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=config.timeout_seconds,
        )
    except requests.Timeout as exc:
        raise SummaryError(
            f"Gemini API call timed out for model {model}.", last_model=model
        ) from exc
    except requests.RequestException as exc:
        raise SummaryError(
            f"Gemini API request failed for model {model}: {exc}", last_model=model
        ) from exc

    if response.status_code != 200:
        error_message = _extract_error_message(response)
        if response.status_code in {429, 503} or _is_quota_error(error_message):
            raise _ModelQuotaError(
                f"Gemini model {model} exhausted or unavailable: {error_message}",
                last_model=model,
            )
        raise SummaryError(
            f"Gemini API returned status {response.status_code} for {model}: {error_message}",
            last_model=model,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise SummaryError("Invalid JSON from Gemini API.", last_model=model) from exc

    summary = _extract_summary_text(data)
    if not summary:
        raise SummaryError("Gemini API returned no summary text.", last_model=model)

    if config.debug:
        print("[GEMINI DEBUG] Summary:", file=sys.stderr)
        print(summary, file=sys.stderr)

    return summary


def _extract_summary_text(data: dict) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return ""

    for candidate in candidates:
        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        texts: List[str] = []
        if isinstance(parts, list):
            for part in parts:
                text = part.get("text") if isinstance(part, dict) else None
                if text:
                    texts.append(text)
        if texts:
            combined = "\n".join(texts).strip()
            if combined:
                return combined

        # 일부 응답은 직접 text 필드를 포함한다.
        text_fallback = candidate.get("text")
        if isinstance(text_fallback, str) and text_fallback.strip():
            return text_fallback.strip()

    return ""


def _extract_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        status = error.get("status")
        if message and status:
            return f"{status}: {message}"
        if message:
            return message
    return json.dumps(payload, ensure_ascii=False)


def _is_quota_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(keyword in lowered for keyword in ("quota", "exhaust", "429", "rate"))


def _redact_image_data(payload: dict) -> dict:
    """이미지 base64 데이터를 제외한 디버그용 사본을 만든다."""
    redacted = json.loads(json.dumps(payload))  # deep copy
    contents = redacted.get("contents")
    if not isinstance(contents, list):
        return redacted
    for content in contents:
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inline_data")
            if isinstance(inline, dict) and "data" in inline:
                inline["data"] = "<omitted>"
    return redacted
