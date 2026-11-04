"""Service-to-service outcome publisher; called by a real business outcome process."""

import time
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class OutcomePublisher:
    """Retries transient failures with the same event ID; never infers actual outcomes."""

    def __init__(self, url: str, token: str, *, attempts: int = 3) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username
            or parsed.password
            or not 1 <= attempts <= 5
            or len(token) < 32
        ):
            raise ValueError(
                "Publisher requires a loopback monitor, service credential and bounded retries"
            )
        self.url = url.rstrip("/") + "/api/integrations/outcomes"
        self._token = token
        self.attempts = attempts

    def publish(
        self,
        *,
        event_id: UUID,
        observation_id: UUID,
        model_version: str,
        actual: int,
        evidence_ref: str,
        occurred_at: datetime,
    ) -> None:
        if type(actual) is not int or actual not in (0, 1):
            raise ValueError("A verified binary outcome is required")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("Business event timestamps must have a timezone")
        payload = dict(
            event_id=str(event_id),
            observation_id=str(observation_id),
            model_version=model_version,
            actual=actual,
            evidence_ref=evidence_ref,
            occurred_at=occurred_at.isoformat(),
        )
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            for attempt in range(self.attempts):
                try:
                    response = client.post(
                        self.url, json=payload, headers={"Authorization": "Bearer " + self._token}
                    )
                    if response.status_code < 500 and response.status_code != 429:
                        response.raise_for_status()
                        return
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    if error.response.status_code < 500 and error.response.status_code != 429:
                        raise
                    if attempt + 1 == self.attempts:
                        raise
                except httpx.TransportError:
                    if attempt + 1 == self.attempts:
                        raise
                time.sleep(min(0.25 * 2**attempt, 2))
