"""공통 RabbitMQ 워커 유틸리티."""

from __future__ import annotations

import logging
import os
import signal
from dataclasses import dataclass
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

    def consume_forever(
        self, handler: Callable[[bytes], MessageHandlingResult]
    ) -> None:
        connection: pika.BlockingConnection | None = None
        channel: pika.channel.Channel | None = None
        try:
            connection = pika.BlockingConnection(self.parameters)
            channel = connection.channel()
            channel.basic_qos(prefetch_count=1)

            def _callback(ch, method, _properties, body):
                try:
                    handler(body)
                except MessageHandlingError as exc:
                    logger.error("Message handling failed: %s", exc, exc_info=True)
                    ch.basic_nack(method.delivery_tag, requeue=exc.requeue)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Unhandled exception during message processing")
                    ch.basic_nack(method.delivery_tag, requeue=True)
                else:
                    ch.basic_ack(method.delivery_tag)

            channel.basic_consume(queue=self.queue_name, on_message_callback=_callback)
            channel.start_consuming()
        finally:
            try:
                if channel and channel.is_open:
                    channel.close()
            finally:
                if connection and connection.is_open:
                    connection.close()


def serve(
    worker_name: str,
    client: RabbitMQClient,
    message_handler: Callable[[bytes], MessageHandlingResult],
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] {worker_name}: %(message)s",
    )
    logger.info("Starting %s worker (long running)", worker_name)

    def _handle_signal(_signum: int, _frame: Optional[FrameType]) -> None:
        logger.info("Received shutdown signal. Exiting...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        client.consume_forever(message_handler)
    except KeyboardInterrupt:
        logger.info("Worker stopped.")
