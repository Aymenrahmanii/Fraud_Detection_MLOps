"""Backbone reliability (v5 section 18): idempotency, retry/backoff, DLQ."""

import json

import pytest
from pydantic import ValidationError

from aiops_platform.bus import (
    InMemoryBus,
    InMemoryIdempotencyStore,
    PermanentError,
    Record,
    ReliableConsumer,
    RetryPolicy,
    publish,
)
from aiops_platform.contracts.enums import Environment, Priority, Severity
from aiops_platform.contracts.events import EventEnvelope, ServiceIdentity
from aiops_platform.contracts.topics import dlq_topic, retry_topic

pytestmark = pytest.mark.asyncio

TOPIC = "monitoring.anomaly.detected"


def anomaly(message_id="MSG-1", correlation_id="INC-1"):
    return EventEnvelope(
        message_id=message_id,
        correlation_id=correlation_id,
        trace_id="TRACE-1",
        source="monitoring-agent",
        target="orchestrator",
        topic=TOPIC,
        message_type="ANOMALY_DETECTED",
        priority=Priority.P1,
        severity=Severity.HIGH,
        environment=Environment.PRODUCTION,
        service=ServiceIdentity(service_id="payment-service", namespace="prod", version="v2.7"),
        idempotency_key=f"{correlation_id}:ANOMALY_DETECTED:v1",
        payload={"anomaly_id": "ANM-1", "detector": "slo", "anomaly_score": 0.9, "signals": {}},
    )


async def consumer(bus, handler, *, group="orchestrator", attempts=3, no_sleep=None):
    c = ReliableConsumer(
        bus,
        group,
        handler,
        idempotency=InMemoryIdempotencyStore(),
        policy=RetryPolicy(max_attempts=attempts, base_delay_seconds=0.001),
        sleep=no_sleep,
    )
    c.subscribe([TOPIC])
    await bus.start()
    return c


async def test_each_group_receives_each_event_once(no_sleep):
    bus = InMemoryBus()
    seen: dict[str, list[str]] = {"a": [], "b": []}

    async def handler_for(group):
        async def handle(envelope):
            seen[group].append(envelope.message_id)

        return handle

    for group in seen:
        c = ReliableConsumer(bus, group, await handler_for(group), idempotency=InMemoryIdempotencyStore(), sleep=no_sleep)
        c.subscribe([TOPIC])
    await bus.start()
    await publish(bus, anomaly("MSG-1"))
    await publish(bus, anomaly("MSG-2"))
    await bus.drain()
    assert seen == {"a": ["MSG-1", "MSG-2"], "b": ["MSG-1", "MSG-2"]}
    await bus.stop()


async def test_kafka_redelivery_has_no_second_effect(no_sleep):
    bus = InMemoryBus()
    calls = []

    async def handle(envelope):
        calls.append(envelope.message_id)

    await consumer(bus, handle, no_sleep=no_sleep)
    await publish(bus, anomaly())
    await bus.drain()
    await bus.redeliver(bus.topic_records(TOPIC)[0])
    await bus.drain()
    assert calls == ["MSG-1"]
    await bus.stop()


async def test_transient_failure_is_retried_then_succeeds(no_sleep):
    bus = InMemoryBus()
    attempts = []

    async def flaky(envelope):
        attempts.append(envelope.message_id)
        if len(attempts) < 3:
            raise TimeoutError("prometheus timeout")

    await consumer(bus, flaky, no_sleep=no_sleep)
    await publish(bus, anomaly())
    await bus.drain()
    assert len(attempts) == 3
    retries = bus.topic_records(retry_topic(TOPIC, "orchestrator"))
    assert [r.headers["x-attempt"] for r in retries] == ["2", "3"]
    assert bus.topic_records(dlq_topic(TOPIC)) == []
    await bus.stop()


async def test_retry_budget_exhausted_goes_to_dlq_with_metadata(no_sleep):
    bus = InMemoryBus()

    async def broken(envelope):
        raise TimeoutError("K8S_API_TIMEOUT")

    await consumer(bus, broken, attempts=3, no_sleep=no_sleep)
    await publish(bus, anomaly())
    await bus.drain()
    [dead] = bus.topic_records(dlq_topic(TOPIC))
    body = json.loads(dead.value)
    assert body["original_topic"] == TOPIC
    assert body["original_message_id"] == "MSG-1"
    assert body["attempt"] == 3 and body["max_attempts"] == 3
    assert body["last_error"]["retryable"] is True
    assert body["next_destination"] == dlq_topic(TOPIC)
    await bus.stop()


async def test_permanent_error_and_poison_message_skip_retries(no_sleep):
    bus = InMemoryBus()

    async def reject(envelope):
        raise PermanentError("forbidden action")

    await consumer(bus, reject, no_sleep=no_sleep)
    await publish(bus, anomaly())
    await bus.publish_record(Record(topic=TOPIC, key="INC-1", value=b'{"not": "an envelope"}'))
    await bus.drain()
    assert bus.topic_records(retry_topic(TOPIC, "orchestrator")) == []
    codes = [json.loads(r.value)["last_error"]["code"] for r in bus.topic_records(dlq_topic(TOPIC))]
    assert codes == ["PermanentError", "SCHEMA_INVALID"]
    await bus.stop()


async def test_retry_backoff_is_exponential_and_bounded():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=5, jitter_ratio=0)
    assert [policy.delay(a) for a in (1, 2, 3, 4)] == [1, 2, 4, 5]


async def test_envelope_rejects_unknown_topic_and_wrong_type():
    data = anomaly().model_dump()
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({**data, "topic": "nope"})
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({**data, "message_type": "DIAGNOSIS_COMPLETED"})
