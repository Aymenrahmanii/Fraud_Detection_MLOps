"""In-process bus with Kafka's consumer-group semantics, for tests and local runs.

Each (group, topic) pair has its own queue, so every group receives every
record once and processes a topic in publish order. `drain()` waits until no
record is queued or in flight, which makes asynchronous workflows testable.
Every published record is kept in `published` for assertions and replay.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence

from aiops_platform.bus.base import Record, RecordHandler

log = logging.getLogger(__name__)


class InMemoryBus:
    def __init__(self) -> None:
        self.published: list[Record] = []
        self._subscriptions: dict[str, list[tuple[str, RecordHandler]]] = defaultdict(list)
        self._queues: dict[tuple[str, str], asyncio.Queue[Record]] = {}
        self._handlers: dict[tuple[str, str], RecordHandler] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._pending = 0
        self._idle: asyncio.Event | None = None
        self._started = False

    def subscribe(self, topics: Sequence[str], group: str, handler: RecordHandler) -> None:
        for topic in topics:
            key = (group, topic)
            if key in self._handlers:
                raise ValueError(f"group {group} already consumes {topic}")
            self._handlers[key] = handler
            self._subscriptions[topic].append((group, handler))
            self._queues[key] = asyncio.Queue()
            if self._started:
                self._tasks.append(asyncio.create_task(self._consume(key)))

    async def publish_record(self, record: Record) -> None:
        self.published.append(record)
        for group, _ in self._subscriptions.get(record.topic, ()):
            self._pending += 1
            self._idle_event().clear()
            await self._queues[(group, record.topic)].put(record)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._tasks = [asyncio.create_task(self._consume(key)) for key in self._queues]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait until every queued record has been handled (including retries)."""
        if self._pending == 0:
            return
        await asyncio.wait_for(self._idle_event().wait(), timeout)

    def topic_records(self, topic: str) -> list[Record]:
        return [r for r in self.published if r.topic == topic]

    async def redeliver(self, record: Record) -> None:
        """Simulate Kafka at-least-once redelivery of an already-delivered record."""
        await self.publish_record(record)

    def _idle_event(self) -> asyncio.Event:
        if self._idle is None:
            self._idle = asyncio.Event()
            self._idle.set()
        return self._idle

    async def _consume(self, key: tuple[str, str]) -> None:
        queue = self._queues[key]
        handler = self._handlers[key]
        while True:
            record = await queue.get()
            try:
                await handler(record)
            except Exception:  # noqa: BLE001 - a raw handler bug must not kill the consumer
                log.exception("unhandled error in %s on %s", *key)
            finally:
                queue.task_done()
                self._pending -= 1
                if self._pending == 0:
                    self._idle_event().set()
