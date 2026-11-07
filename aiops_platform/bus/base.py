"""Transport-neutral event bus interface (v5 section 3).

A bus moves `Record`s: a topic, a partition key, a JSON body and string
headers. Envelope validation and the reliability policy (dedup, retry, DLQ) sit
above the transport in `reliability.py`, so the in-memory bus and the Kafka bus
behave the same way.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from aiops_platform.contracts.events import EventEnvelope


@dataclass(frozen=True)
class Record:
    topic: str
    key: str
    value: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_envelope(cls, envelope: EventEnvelope, headers: dict[str, str] | None = None) -> "Record":
        return cls(
            topic=envelope.topic,
            key=envelope.partition_key,
            value=envelope.model_dump_json().encode("utf-8"),
            headers=dict(headers or {}),
        )


RecordHandler = Callable[[Record], Awaitable[None]]
EnvelopeHandler = Callable[[EventEnvelope], Awaitable[None]]


class EventBus(Protocol):
    async def publish_record(self, record: Record) -> None: ...

    def subscribe(self, topics: Sequence[str], group: str, handler: RecordHandler) -> None:
        """Register before `start()`; one logical consumer per group (v5 section 3.1)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


async def publish(bus: EventBus, envelope: EventEnvelope) -> None:
    await bus.publish_record(Record.from_envelope(envelope))


class PermanentError(Exception):
    """The message can never succeed (bad schema, forbidden action): DLQ at once."""


class TransientError(Exception):
    """A dependency failed temporarily (timeout, unavailable): retry with backoff."""
