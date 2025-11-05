"""공통 RabbitMQ 워커 유틸리티."""

from __future__ import annotations

import json
import logging
import os
import signal
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Callable, Optional

import pika


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageHandlingResult:
    """메시지 처리 결과."""

    processed: bool
    message: str


class MessageHandlingError(Exception):
    """메시지 처리에 실패했음을 나타내며 재시도 여부를 포함한다."""

    def __init__(self, message: str, *, requeue: bool = False) -> None:
        super().__init__(message)
        self.requeue = requeue


def getenv_casefold(key: str) -> Optional[str]:
    target = key.casefold()
    for env_key, value in os.environ.items():
        if env_key.casefold() == target:
            return value
    return None


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as env_file:
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


def env_flag(key: str, default: bool = False) -> bool:
    value = getenv_casefold(key)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "off", "no"}


def env_int(key: str, default: int) -> int:
    value = getenv_casefold(key)
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


class RabbitMQClient:
    """RabbitMQ 연결 헬퍼."""

    def __init__(self, queue_name: str) -> None:
        self.queue_name = queue_name
        url = getenv_casefold("RABBITMQ_URL") or "amqp://guest:guest@localhost:5672/%2F"
        self.parameters = pika.URLParameters(url)

    def consume_one(
        self, handler: Callable[[bytes], MessageHandlingResult]
    ) -> MessageHandlingResult:
        connection: pika.BlockingConnection | None = None
        channel: pika.channel.Channel | None = None
        try:
            connection = pika.BlockingConnection(self.parameters)
            channel = connection.channel()
            channel.basic_qos(prefetch_count=1)
            method_frame, _header_frame, body = channel.basic_get(
                queue=self.queue_name, auto_ack=False
            )
            if method_frame is None:
                return MessageHandlingResult(False, "queue empty")

            try:
                result = handler(body)
            except MessageHandlingError as exc:
                logger.error("Message handling failed: %s", exc, exc_info=True)
                if channel.is_open:
                    channel.basic_nack(method_frame.delivery_tag, requeue=exc.requeue)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unhandled exception during message processing")
                if channel.is_open:
                    channel.basic_nack(method_frame.delivery_tag, requeue=True)
                raise MessageHandlingError(str(exc), requeue=True) from exc
            else:
                if channel.is_open:
                    channel.basic_ack(method_frame.delivery_tag)
                return result
        finally:
            try:
                if channel and channel.is_open:
                    channel.close()
            finally:
                if connection and connection.is_open:
                    connection.close()


def _build_handler(
    client: RabbitMQClient, message_handler: Callable[[bytes], MessageHandlingResult]
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            if content_length:
                _ = self.rfile.read(content_length)
            try:
                result = client.consume_one(message_handler)
            except MessageHandlingError as exc:
                status = (
                    HTTPStatus.INTERNAL_SERVER_ERROR
                    if exc.requeue
                    else HTTPStatus.BAD_REQUEST
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                payload = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.wfile.write(payload)
                return
            except Exception as exc:  # noqa: BLE001
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                payload = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.wfile.write(payload)
                return

            if not result.processed:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            payload = json.dumps({"ok": True, "message": result.message}).encode(
                "utf-8"
            )
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003, D401
            logger.info("%s - %s", self.address_string(), format % args)

    return Handler


def serve(
    worker_name: str,
    client: RabbitMQClient,
    message_handler: Callable[[bytes], MessageHandlingResult],
) -> None:
    port = env_int("PORT", 8080)
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] {worker_name}: %(message)s",
    )
    handler = _build_handler(client, message_handler)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    logger.info("Starting %s worker on port %s", worker_name, port)

    def _handle_signal(_signum: int, _frame: Optional[FrameType]) -> None:
        logger.info("Received shutdown signal. Stopping server...")
        server.shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        server.serve_forever()
    finally:
        server.server_close()
