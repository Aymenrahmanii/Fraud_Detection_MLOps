"""JSON encoding of contract values for the gRPC `*_json` fields."""

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

DATETIME_KEYS = {"created_at", "expires_at", "resolved_at", "observed_at", "opened_at", "updated_at", "closed_at"}


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def dumps(value: Any) -> str:
    return json.dumps(value, default=_default, allow_nan=False)


def loads(text: str) -> Any:
    return json.loads(text) if text else None


def revive_datetimes(row: dict[str, Any]) -> dict[str, Any]:
    """Approval/audit rows carry datetimes; JSON carries ISO strings."""
    out = dict(row)
    for key in DATETIME_KEYS & out.keys():
        if isinstance(out[key], str):
            out[key] = datetime.fromisoformat(out[key])
    return out
