"""Transport-neutral, at-least-once delivery for the transactional outbox."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from agent4_deployment_recovery.core.integration_models import (
    ClaimedOutboxDelivery,
)
from agent4_deployment_recovery.persistence.sqlite_journal import SQLiteExecutionJournal


@runtime_checkable
class OutboxTransport(Protocol):
    def deliver(self, delivery: ClaimedOutboxDelivery) -> None:
        """Deliver once per claim; consumers must deduplicate by delivery_id."""


class InMemoryOutboxTransport:
    """Test-only transport that records attempts and can inject failures."""

    def __init__(self, fail: Callable[[ClaimedOutboxDelivery], bool] | None = None):
        self._fail = fail or (lambda delivery: False)
        self.attempts: list[ClaimedOutboxDelivery] = []

    def deliver(self, delivery: ClaimedOutboxDelivery) -> None:
        self.attempts.append(delivery)
        if self._fail(delivery):
            raise RuntimeError("injected outbox transport failure")


class OutboxDispatcher:
    """Claim, deliver, and explicitly acknowledge a bounded event batch."""

    def __init__(
        self,
        *,
        journal: SQLiteExecutionJournal,
        transport: OutboxTransport,
        worker_id: str,
        lease_seconds: int = 30,
        max_attempts: int = 5,
        batch_size: int = 10,
        now: Callable[[], datetime] | None = None,
    ):
        self._journal = journal
        self._transport = transport
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._batch_size = batch_size
        self._now = now or (lambda: datetime.now(UTC))

    def dispatch_once(self) -> tuple[int, int]:
        """Return delivered and rejected counts; never claim remote exactly-once delivery."""
        claims = self._journal.claim_outbox(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
            limit=self._batch_size,
            now=self._now(),
        )
        delivered = 0
        rejected = 0
        for delivery in claims:
            try:
                self._transport.deliver(delivery)
            # A transport is an untrusted plugin boundary and may raise any ordinary
            # exception. Convert it into a durable retry outcome; BaseException still
            # propagates so process termination is never mistaken for a rejection.
            except Exception as exc:  # noqa: BLE001
                self._journal.reject_outbox(
                    delivery,
                    error=str(exc) or type(exc).__name__,
                    max_attempts=self._max_attempts,
                    now=self._now(),
                )
                rejected += 1
            else:
                self._journal.acknowledge_outbox(delivery, now=self._now())
                delivered += 1
        return delivered, rejected
