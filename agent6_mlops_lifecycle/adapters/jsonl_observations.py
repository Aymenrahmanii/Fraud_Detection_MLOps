"""Read bounded, independently recorded predictions and outcomes from JSONL."""

import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from agent6_mlops_lifecycle.contracts.base import Contract
from agent6_mlops_lifecycle.contracts.mlops_ingestion import OutcomeRecord, PredictionRecord
from agent6_mlops_lifecycle.interfaces.observations import ObservationSnapshot

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_RECORDS = 50_000
MAX_LINE_BYTES = 64 * 1024


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _read[RecordT: Contract](
    path: Path, record_type: type[RecordT]
) -> tuple[tuple[RecordT, ...], str]:
    # Read once and hash these exact bytes, even if the file is replaced later.
    with path.open("rb") as stream:
        contents = stream.read(MAX_SOURCE_BYTES + 1)
    if len(contents) > MAX_SOURCE_BYTES:
        raise ValueError("Observation source exceeds the byte limit")
    records: list[RecordT] = []
    for line_number, line in enumerate(BytesIO(contents), start=1):
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            raise ValueError(f"Observation source line {line_number} exceeds the byte limit")
        if len(records) >= MAX_SOURCE_RECORDS:
            raise ValueError("Observation source exceeds the record limit")
        try:
            document = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_unique_keys,
                parse_constant=_reject_constant,
            )
            records.append(record_type.model_validate(document))
        except (ValueError, UnicodeError) as error:
            # Do not echo prediction values or raw source lines in errors.
            raise ValueError(
                f"Invalid {record_type.__name__} at source line {line_number}"
            ) from error
    return tuple(records), f"sha256:{sha256(contents).hexdigest()}"


class JSONLObservationSource:
    def __init__(self, predictions_file: Path, outcomes_file: Path) -> None:
        self.predictions_file = predictions_file
        self.outcomes_file = outcomes_file

    def read_snapshot(self) -> ObservationSnapshot:
        predictions, prediction_ref = _read(self.predictions_file, PredictionRecord)
        outcomes, outcome_ref = _read(self.outcomes_file, OutcomeRecord)
        return ObservationSnapshot(predictions, outcomes, prediction_ref, outcome_ref)
