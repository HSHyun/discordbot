"""Codex CLI를 통한 요약 생성을 담당한다."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from typing import List


class SummaryError(Exception):
    """Codex CLI 요약이 실패했을 때 발생한다."""


@dataclass(frozen=True)
class CodexConfig:
    """Codex 요약 호출에 필요한 설정."""

    model: str
    timeout_seconds: int
    max_text_length: int
    debug: bool = False


def summarise_with_codex(
    text: str,
    image_paths: List[str],
    config: CodexConfig,
) -> str:
    """Codex CLI를 호출해 텍스트와 이미지를 요약한다."""
    text_to_use = text.strip()
    if not text_to_use:
        if image_paths:
            text_to_use = "(본문 텍스트 없음 — 이미지를 기반으로 요약해 주세요.)"
        else:
            raise SummaryError("No text or images available for summarisation.")

    if len(text_to_use) > config.max_text_length:
        text_to_use = text_to_use[: config.max_text_length] + "\n..."

    system_prompt = textwrap.dedent(
        """
        당신은 DCInside 게시물을 한국어로 요약하는 전문가입니다. 출력은 반드시 자연스러운 한국어 문장으로만 작성하며,
        불릿이나 영어 문장은 사용하지 않습니다. 답변은 3문장 이내로 유지하고, 링크는 제외하며, 이미지에서 확인한 중요한
        사실이 있다면 텍스트의 맥락 속에 자연스럽게 통합합니다. 인물 이름이나 고유명사는 원문을 그대로 유지합니다.
        """
    ).strip()

    user_prompt = textwrap.dedent(
        f"""
        아래는 게시물 원문입니다. 필요하다면 첨부 이미지를 참고해 주세요.

        {text_to_use}
        """
    ).strip()

    messages = [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": system_prompt}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": user_prompt}],
        },
    ]

    stdin_payload = (
        "\n".join(json.dumps(message, ensure_ascii=False) for message in messages)
        + "\n"
    )

    cmd = [
        "codex",
        "exec",
        "--experimental-json",
        "--model",
        config.model,
        "--config",
        "agent.run_commands=false",
        "--config",
        "agent.plan=false",
    ]
    for path in image_paths:
        cmd.extend(["--image", path])

    if config.debug:
        debug_lines = [
            "[CODEX DEBUG] Model: " + config.model,
            "[CODEX DEBUG] Prompt JSON:",
            stdin_payload,
        ]
        if image_paths:
            debug_lines.append("[CODEX DEBUG] Images: " + ", ".join(image_paths))
        print("\n".join(debug_lines), file=sys.stderr)

    try:
        process = subprocess.run(
            cmd,
            input=stdin_payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SummaryError("Codex CLI call timed out.") from exc

    if process.returncode != 0:
        stderr_text = process.stderr.decode("utf-8", errors="replace")
        raise SummaryError(
            f"Codex CLI failed with exit code {process.returncode}: {stderr_text}"
        )

    raw_output = process.stdout.decode("utf-8", errors="replace")

    summary_messages: List[str] = []
    stream_buffers: dict[str, List[str]] = {}
    errors: List[str] = []
    for line in raw_output.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = message.get("type")
        if msg_type == "item.completed":
            item = message.get("item") or {}
            if item.get("item_type") == "assistant_message":
                text = (item.get("text") or "").strip()
                if text:
                    summary_messages.append(text)
        elif msg_type == "item.delta":
            item = message.get("item") or {}
            if item.get("item_type") == "assistant_message":
                text = item.get("text") or ""
                item_id = item.get("id") or "__default__"
                if text:
                    stream_buffers.setdefault(item_id, []).append(text)
        elif msg_type == "response.output_text.delta":
            delta = message.get("delta") or {}
            text = delta.get("text")
            response_id = delta.get("id") or "__default_response__"
            if text:
                stream_buffers.setdefault(response_id, []).append(text)
        elif msg_type == "response.completed":
            response = message.get("response") or {}
            response_id = response.get("id")
            if response_id and response_id in stream_buffers:
                combined = "".join(stream_buffers.pop(response_id)).strip()
                if combined:
                    summary_messages.append(combined)
            else:
                output = response.get("output_text") or {}
                text = (output.get("final") or {}).get("text")
                if text:
                    summary_messages.append(text.strip())
        elif msg_type == "error":
            err_msg = message.get("message")
            if err_msg:
                errors.append(err_msg)

    for leftover in stream_buffers.values():
        combined = "".join(leftover).strip()
        if combined:
            summary_messages.append(combined)

    cleaned_messages: List[str] = []
    for msg in summary_messages:
        if not msg:
            continue
        if not cleaned_messages or cleaned_messages[-1] != msg:
            cleaned_messages.append(msg)

    summary = "\n\n".join(cleaned_messages).strip()

    if not summary:
        if errors:
            raise SummaryError(f"Codex CLI reported error: {errors[-1]}")
        raise SummaryError("Codex CLI returned no assistant message.")

    if config.debug:
        print("[CODEX DEBUG] Summary:", file=sys.stderr)
        print(summary, file=sys.stderr)

    return summary
