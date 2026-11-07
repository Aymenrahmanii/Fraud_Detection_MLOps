"""Processed-message store: at-least-once delivery, exactly-once effect (v5 section 18)."""

import time
from typing import Protocol


class IdempotencyStore(Protocol):
    async def seen(self, key: str) -> bool: ...

    async def mark(self, key: str, ttl_seconds: int = 7 * 24 * 3600) -> None: ...


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._keys: dict[str, float] = {}

    async def seen(self, key: str) -> bool:
        expiry = self._keys.get(key)
        if expiry is None:
            return False
        if expiry < time.monotonic():
            del self._keys[key]
            return False
        return True

    async def mark(self, key: str, ttl_seconds: int = 7 * 24 * 3600) -> None:
        self._keys[key] = time.monotonic() + ttl_seconds


class RedisIdempotencyStore:
    """`idem:{key}` in Redis, as in the v5 storage table (section 6.2)."""

    def __init__(self, client) -> None:  # redis.asyncio.Redis
        self._redis = client

    async def seen(self, key: str) -> bool:
        return bool(await self._redis.exists(f"idem:{key}"))

    async def mark(self, key: str, ttl_seconds: int = 7 * 24 * 3600) -> None:
        await self._redis.set(f"idem:{key}", "1", ex=ttl_seconds)
