"""Reliable consumption on top of any `EventBus` (v5 sections 3.1 and 18).

For each subscribed topic a consumer group gets:

* schema validation of the envelope (invalid -> DLQ, never retried);
* deduplication by `group:message_id` (Kafka redelivery has no second effect);
* bounded retries through `<topic>.retry.<group>` with exponential backoff and
  jitter; the retry carries the attempt count in headers;
* a dead-letter record on `<topic>.dlq` with the v5 section 18.1 metadata once
  the retry budget is spent, or immediately for a `PermanentError`.
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from aiops_platform.bus.base import (
    EnvelopeHandler,
    EventBus,
    PermanentError,
    Record,
)
from aiops_platform.bus.idempotency import IdempotencyStore
from aiops_platform.contracts.events import EventEnvelope
from aiops_platform.contracts.topics import dlq_topic, retry_topic

log = logging.getLogger(__name__)

H_ATTEMPT = "x-attempt"
H_NOT_BEFORE = "x-not-before"
H_ORIGINAL_TOPIC = "x-original-topic"
H_FIRST_FAILED = "x-first-failed-at"
H_LAST_ERROR = "x-last-error"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def delay(self, attempt: int) -> float:
        """Delay before attempt `attempt + 1`, with +/- jitter."""
        raw = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (attempt - 1))
        return max(0.0, raw * (1 + random.uniform(-self.jitter_ratio, self.jitter_ratio)))


class ConsumerObserver:
    """Hook for platform metrics; the default does nothing."""

    def processed(self, group: str, topic: str, seconds: float) -> None: ...
    def duplicate(self, group: str, topic: str) -> None: ...
    def retried(self, group: str, topic: str, attempt: int) -> None: ...
    def dead_lettered(self, group: str, topic: str, code: str) -> None: ...


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _error_code(exc: BaseException) -> str:
    return getattr(exc, "code", None) or type(exc).__name__


class ReliableConsumer:
    def __init__(
        self,
        bus: EventBus,
        group: str,
        handler: EnvelopeHandler,
        *,
        idempotency: IdempotencyStore,
        policy: RetryPolicy | None = None,
        observer: ConsumerObserver | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bus = bus
        self._group = group
        self._handler = handler
        self._idempotency = idempotency
        self._policy = policy or RetryPolicy()
        self._observer = observer or ConsumerObserver()
        self._sleep = sleep
        self._clock = clock

    def subscribe(self, topics: Sequence[str]) -> None:
        self._bus.subscribe(list(topics), self._group, self._on_record)
        self._bus.subscribe(
            [retry_topic(t, self._group) for t in topics], self._group, self._on_retry
        )

    async def _on_record(self, record: Record) -> None:
        await self._process(record, attempt=1, original_topic=record.topic)

    async def _on_retry(self, record: Record) -> None:
        not_before = float(record.headers.get(H_NOT_BEFORE, "0"))
        wait = not_before - self._clock()
        if wait > 0:
            await self._sleep(wait)
        await self._process(
            record,
            attempt=int(record.headers.get(H_ATTEMPT, "1")),
            original_topic=record.headers.get(H_ORIGINAL_TOPIC, record.topic),
        )

    async def _process(self, record: Record, *, attempt: int, original_topic: str) -> None:
        try:
            envelope = EventEnvelope.model_validate_json(record.value)
        except ValidationError as exc:
            await self._dead_letter(record, original_topic, attempt, "SCHEMA_INVALID", str(exc), False)
            return
        key = f"{self._group}:{envelope.message_id}"
        if await self._idempotency.seen(key):
            self._observer.duplicate(self._group, original_topic)
            log.info("duplicate %s ignored by %s", envelope.message_id, self._group)
            return
        started = time.perf_counter()
        try:
            await self._handler(envelope)
        except PermanentError as exc:
            await self._dead_letter(record, original_topic, attempt, _error_code(exc), str(exc), False)
            await self._idempotency.mark(key)
            return
        except Exception as exc:  # noqa: BLE001 - every other failure is retried, then dead-lettered
            log.warning("%s failed on %s (attempt %d): %s", self._group, original_topic, attempt, exc)
            if attempt >= self._policy.max_attempts:
                await self._dead_letter(record, original_topic, attempt, _error_code(exc), str(exc), True)
                await self._idempotency.mark(key)
                return
            await self._schedule_retry(record, original_topic, attempt, exc)
            return
        await self._idempotency.mark(key)
        self._observer.processed(self._group, original_topic, time.perf_counter() - started)

    async def _schedule_retry(
        self, record: Record, original_topic: str, attempt: int, exc: BaseException
    ) -> None:
        headers = dict(record.headers)
        headers.setdefault(H_FIRST_FAILED, _now_iso())
        headers.update(
            {
                H_ATTEMPT: str(attempt + 1),
                H_NOT_BEFORE: str(self._clock() + self._policy.delay(attempt)),
                H_ORIGINAL_TOPIC: original_topic,
                H_LAST_ERROR: json.dumps({"code": _error_code(exc), "message": str(exc)[:500]}),
            }
        )
        self._observer.retried(self._group, original_topic, attempt)
        await self._bus.publish_record(
            Record(
                topic=retry_topic(original_topic, self._group),
                key=record.key,
                value=record.value,
                headers=headers,
            )
        )

    async def _dead_letter(
        self,
        record: Record,
        original_topic: str,
        attempt: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        try:
            original_id = json.loads(record.value).get("message_id")
        except (ValueError, AttributeError):
            original_id = None
        body = {
            "original_topic": original_topic,
            "original_message_id": original_id,
            "consumer_group": self._group,
            "attempt": attempt,
            "max_attempts": self._policy.max_attempts,
            "last_error": {"code": code, "message": message[:2000], "retryable": retryable},
            "first_failed_at": record.headers.get(H_FIRST_FAILED, _now_iso()),
            "last_failed_at": _now_iso(),
            "next_destination": dlq_topic(original_topic),
            "original_value": record.value.decode("utf-8", errors="replace"),
        }
        self._observer.dead_lettered(self._group, original_topic, code)
        log.error("dead-lettered %s from %s: %s", original_id, original_topic, code)
        await self._bus.publish_record(
            Record(
                topic=dlq_topic(original_topic),
                key=record.key,
                value=json.dumps(body).encode("utf-8"),
                headers={"x-consumer-group": self._group},
            )
        )
