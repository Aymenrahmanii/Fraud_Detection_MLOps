from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent4_deployment_recovery.core.canonical import canonical_json, canonical_sha256


def test_hash_is_stable_across_mapping_order_and_timezone_offsets():
    instant = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    offset_instant = instant.astimezone(timezone(timedelta(hours=1)))
    first = {"timestamp": instant, "nested": {"b": 2, "a": 1}}
    second = {"nested": {"a": 1, "b": 2}, "timestamp": offset_instant}

    assert canonical_json(first) == canonical_json(second)
    assert canonical_sha256(first) == canonical_sha256(second)


def test_canonical_mapping_rejects_non_string_keys_to_prevent_key_collisions():
    with pytest.raises(TypeError, match="string keys"):
        canonical_json({1: "integer", "1": "string"})
