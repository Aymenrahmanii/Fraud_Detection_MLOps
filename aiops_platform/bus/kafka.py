"""Kafka transport (aiokafka); Redpanda in `compose.platform.yaml`.

* Producer: `acks=all` + idempotent producer; the record key is the
  envelope's partition key (`correlation_id`), so one incident stays ordered.
* Consumers: one per group, manual commit **after** the handler returns, giving
  at-least-once delivery. `ReliableConsumer` turns that into one effect.
* Topics are created at start-up with the configured partition count.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence

from aiops_platform.bus.base import Record, RecordHandler

log = logging.getLogger(__name__)


class KafkaBus:
    def __init__(
        self,
        bootstrap_servers: str,
        *,
        client_id: str = "aiops-platform",
        partitions: int = 3,
        replication_factor: int = 1,
    ) -> None:
        self._bootstrap = bootstrap_servers
        self._client_id = client_id
        self._partitions = partitions
        self._replication = replication_factor
        self._groups: dict[str, dict[str, RecordHandler]] = defaultdict(dict)
        self._producer = None
        self._consumers: list = []
        self._tasks: list[asyncio.Task[None]] = []

    def subscribe(self, topics: Sequence[str], group: str, handler: RecordHandler) -> None:
        for topic in topics:
            if topic in self._groups[group]:
                raise ValueError(f"group {group} already consumes {topic}")
            self._groups[group][topic] = handler

    async def publish_record(self, record: Record) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaBus.start() must be awaited before publishing")
        await self._producer.send_and_wait(
            record.topic,
            value=record.value,
            key=record.key.encode("utf-8"),
            headers=[(k, v.encode("utf-8")) for k, v in record.headers.items()],
        )

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        await self.ensure_topics({t for handlers in self._groups.values() for t in handlers})
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            acks="all",
            enable_idempotence=True,
        )
        await self._producer.start()
        for group, handlers in self._groups.items():
            consumer = AIOKafkaConsumer(
                *handlers,
                bootstrap_servers=self._bootstrap,
                group_id=group,
                client_id=f"{self._client_id}-{group}",
                enable_auto_commit=False,
                auto_offset_reset="earliest",
            )
            await consumer.start()
            self._consumers.append(consumer)
            self._tasks.append(asyncio.create_task(self._consume(consumer, handlers)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for consumer in self._consumers:
            await consumer.stop()
        if self._producer is not None:
            await self._producer.stop()
        self._tasks.clear()
        self._consumers.clear()
        self._producer = None

    async def ensure_topics(self, topics: set[str]) -> None:
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic
        from aiokafka.errors import TopicAlreadyExistsError

        if not topics:
            return
        admin = AIOKafkaAdminClient(bootstrap_servers=self._bootstrap, client_id=self._client_id)
        await admin.start()
        try:
            existing = set(await admin.list_topics())
            missing = sorted(topics - existing)
            if missing:
                try:
                    await admin.create_topics(
                        [NewTopic(t, self._partitions, self._replication) for t in missing]
                    )
                except TopicAlreadyExistsError:
                    pass
        finally:
            await admin.close()

    async def _consume(self, consumer, handlers: dict[str, RecordHandler]) -> None:
        async for message in consumer:
            record = Record(
                topic=message.topic,
                key=(message.key or b"").decode("utf-8"),
                value=message.value,
                headers={k: v.decode("utf-8") for k, v in (message.headers or ())},
            )
            try:
                await handlers[message.topic](record)
            except Exception:  # noqa: BLE001 - ReliableConsumer already handles domain errors
                log.exception("unhandled error consuming %s", message.topic)
            # Commit after handling: a crash before this line redelivers the record.
            await consumer.commit()
