import json
from pathlib import Path

import pytest

from agent1_monitoring.shared_contracts.events import EventEnvelope

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anomaly() -> EventEnvelope:
    path = ROOT / "shared_contracts/examples/v1/monitoring.anomaly.detected.json"
    return EventEnvelope.model_validate(json.loads(path.read_text(encoding="utf-8")))
