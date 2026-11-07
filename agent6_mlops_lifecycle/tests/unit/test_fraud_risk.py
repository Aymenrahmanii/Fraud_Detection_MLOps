"""Known confusion/cost cases, ranking ties, probability calibration and business feed joins."""

import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from API.telemetry.business_events import BusinessEvent, receive_event
from API.telemetry.contracts import Scope
from API.telemetry.store import SQLitePredictionLog
from pydantic import SecretStr

from agent6_mlops_lifecycle.fraud.metrics import confusion, probability_quality, wilson
from agent6_mlops_lifecycle.fraud.models import FraudSettings
from agent6_mlops_lifecycle.fraud.service import assess_fraud
from agent6_mlops_lifecycle.monitoring.models import MonitorScope
from agent6_mlops_lifecycle.monitoring.outcome_feed import poll_outcomes
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings
from agent6_mlops_lifecycle.monitoring.source import DirectObservationSource, SourceUnavailableError
from agent6_mlops_lifecycle.monitoring.state import MonitorState

FEATURES = dict(
    Category="shopping",
    paymentMethod="paypal",
    isWeekend=0.0,
    numItems=1,
    localTime=4.7,
    paymentMethodAgeDays=0.0,
    accountAgeDays=1,
)


def policy(**kwargs):
    return FraudSettings(_env_file=None, **kwargs)


def test_missed_fraud_false_alarms_and_explicit_costs():
    p = policy(false_negative_cost=100, false_positive_cost=2, cost_unit="EUR")
    m = confusion([(1, 1), (1, 0), (0, 1), (0, 0)], p)
    assert (m.true_positives, m.false_positives, m.false_negatives, m.true_negatives) == (
        1,
        1,
        1,
        1,
    )
    assert m.recall == m.false_positive_rate == m.missed_fraud_rate == 0.5
    assert m.estimated_error_cost == 102 and m.cost_per_1000 == 25500
    assert confusion([(1, 0)], policy()).estimated_error_cost is None
    assert confusion([], p).cost_per_1000 is None
    interval = wilson(1, 2)
    assert interval[0] < 0.5 < interval[1]
    assert wilson(0, 0) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"false_negative_cost": 1},
        {"false_positive_cost": 2},
        {"max_cost_per_1000": 20},
        {"false_negative_cost": -1, "false_positive_cost": 0},
    ],
)
def test_cost_configuration_cannot_silently_invent_a_price(changes):
    with pytest.raises(ValueError):
        policy(**changes)


@pytest.mark.parametrize(
    "probabilities",
    [[0.1, 0.4, 0.35, 0.8], [0.5, 0.5, 0.5, 0.5], [0.0, 1.0, 0.0, 1.0], [0.9, 0.8, 0.7, 0.6]],
)
def test_ranking_metrics_and_brier_match_independent_library(probabilities):
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

    actual = [0, 0, 1, 1]
    q = probability_quality(list(zip(actual, probabilities, strict=True)), 4, policy())
    assert q.average_precision == pytest.approx(average_precision_score(actual, probabilities))
    assert q.roc_auc == pytest.approx(roc_auc_score(actual, probabilities))
    assert q.brier_score == pytest.approx(brier_score_loss(actual, probabilities))
    assert q.status == "INSUFFICIENT_EVIDENCE"
    assert sum(b.samples for b in q.calibration_bins) == 4


def test_probability_support_requires_each_class_and_high_coverage():
    q = probability_quality([(1, 0.8)] * 5 + [(0, 0.2)] * 25, 30, policy())
    assert q.status == "ASSESSED" and q.average_precision == 1
    assert (
        probability_quality([(1, 0.8)] * 5 + [(0, 0.2)] * 25, 100, policy()).status
        == "INSUFFICIENT_EVIDENCE"
    )
    assert probability_quality([(1, 0.8)] * 30, 30, policy()).average_precision is None
    assert probability_quality([], 10, policy()).status == "UNAVAILABLE"


def test_threshold_cost_advice_is_bounded_and_never_applied():
    q = probability_quality(
        [(0, 0.1)] * 25 + [(1, 0.8)] * 5, 30, policy(false_negative_cost=100, false_positive_cost=1)
    )
    assert q.advisory_cost_threshold is not None and not q.threshold_applied
    assert len(q.threshold_comparison) == 21
    large = probability_quality([(int(i % 2), i / 1999) for i in range(2000)], 2000, policy())
    assert len(large.precision_recall_curve) <= 51


def setup(tmp_path):
    log = SQLitePredictionLog(tmp_path / "app.sqlite3", Scope(), "a" * 64)
    config = MonitoringSettings(
        _env_file=None,
        source_database=log.database,
        state_database=tmp_path / "state.sqlite3",
        evidence_database=tmp_path / "evidence.sqlite3",
        benchmark_database=tmp_path / "benchmark.sqlite3",
        business_database_url=SecretStr("sqlite:///" + str(log.database)),
    )
    source = DirectObservationSource(log.database, MonitorScope())
    state = MonitorState(config.state_database, config.scope())
    return log, config, source, state


def frames(source):
    end = datetime.now(UTC) + timedelta(seconds=1)
    return source.read_frames(end - timedelta(hours=1), end, end)


def test_business_confirmations_join_automatically_and_scores_survive_reopen(tmp_path):
    log, c, source, state = setup(tmp_path)
    for i in range(40):
        y = int(i % 2)
        # Same business ID repeated once must not inflate the evaluation support.
        for _repeat in range(2 if i == 0 else 1):
            log.append(
                y,
                datetime.now(UTC),
                inputs=FEATURES,
                transaction_id=f"order-{i}",
                positive_probability=0.9 if y else 0.1,
                decision_threshold=0.5,
            )
        body = BusinessEvent(
            event_id=uuid4(),
            transaction_id=f"order-{i}",
            source_id="business-service",
            status="FRAUD_CONFIRMED" if y else "LEGITIMATE_CONFIRMED",
            verification_status="VERIFIED",
            evidence_ref=f"case-{i}",
            occurred_at=datetime.now(UTC),
        )
        with log._connect() as db:
            receive_event(db, log.scope, body, ("business-service",))
    assert poll_outcomes(c, source, state).processed_this_poll == 41
    reopened = DirectObservationSource(log.database, MonitorScope())
    r = assess_fraud(
        reopened,
        frames(reopened)[0],
        policy(false_negative_cost=100, false_positive_cost=2, cost_unit="EUR"),
    )
    assert r.status == "PASS" and r.metrics.samples == 40
    assert r.duplicate_transactions_excluded == 1 and r.metrics.false_negatives == 0
    assert r.probabilities.status == "ASSESSED" and r.probabilities.brier_score == pytest.approx(
        0.01
    )
    assert r.metrics.estimated_error_cost == 0 and r.slices
    assert poll_outcomes(c, source, state).processed_this_poll == 0


def test_transaction_database_feed_retains_unmatched_events_without_blocking_later_rows(tmp_path):
    log, c, source, state = setup(tmp_path)
    external = tmp_path / "business.sqlite3"
    with sqlite3.connect(external) as db:
        db.execute(
            "CREATE TABLE confirmations(sequence_id INTEGER PRIMARY KEY,event_id TEXT,"
            "transaction_id TEXT,source_id TEXT,status TEXT,verification_status TEXT,"
            "evidence_ref TEXT,occurred_at TEXT,tenant_id TEXT,project_id TEXT,model_id TEXT)"
        )
        for i in range(2):
            if i == 1:
                log.append(1, datetime.now(UTC), inputs=FEATURES, transaction_id="order-1")
            db.execute(
                "INSERT INTO confirmations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    i + 1,
                    str(uuid4()),
                    f"order-{i}",
                    "business-service",
                    "FRAUD_CONFIRMED",
                    "VERIFIED",
                    f"case-{i}",
                    datetime.now(UTC).isoformat(),
                    c.tenant_id,
                    c.project_id,
                    c.model_id,
                ),
            )
    c = c.model_copy(
        update={
            "business_feed_mode": "transaction",
            "business_outcome_table": "confirmations",
            "business_database_url": SecretStr("sqlite:///" + str(external)),
        }
    )
    status = poll_outcomes(c, source, state)
    assert status.state == "CONNECTED" and status.last_sequence == 2
    assert len(frames(source)[0].snapshot.outcomes) == 1
    old = datetime.now(UTC) - timedelta(minutes=1)
    log.append(0, old, inputs=FEATURES, transaction_id="order-0")
    assert poll_outcomes(c, source, state).state == "CONNECTED"
    assert len(frames(source)[0].snapshot.outcomes) == 2


def test_tampered_inputs_cannot_relabel_a_subgroup(tmp_path):
    log, c, source, state = setup(tmp_path)
    log.append(
        0, datetime.now(UTC), inputs=FEATURES, positive_probability=0.1, decision_threshold=0.5
    )
    with sqlite3.connect(log.database) as db:
        db.execute("UPDATE transactions SET inputs_json='{}'")
    with pytest.raises(SourceUnavailableError, match="digest"):
        assess_fraud(source, frames(source)[0], policy())


def test_legacy_classes_do_not_fabricate_probability_or_slice_evidence(tmp_path):
    from agent6_mlops_lifecycle.monitoring.models import OutcomeSubmission

    log, c, source, state = setup(tmp_path)
    for actual in (0, 1):
        row = log.append(actual, datetime.now(UTC))
        source.submit_outcome(
            row.observation_id,
            OutcomeSubmission(
                actual=actual, note="independently verified test case", verified=True, kind="HUMAN"
            ),
            "test-reviewer",
        )
    r = assess_fraud(
        source,
        frames(source)[0],
        policy(
            min_labels=2, min_positive_labels=1, min_negative_labels=1, min_average_precision=0.5
        ),
    )
    assert r.status == "INSUFFICIENT_EVIDENCE"
    assert r.metrics.samples == 2 and r.metrics.false_negatives == 0
    assert r.probabilities.status == "UNAVAILABLE" and r.probabilities.average_precision is None
    assert r.missing_feature_evidence == 2 and r.slices == ()


def test_simulated_answers_are_excluded_from_fraud_quality(tmp_path):
    from agent6_mlops_lifecycle.monitoring.models import OutcomeSubmission

    log, c, source, state = setup(tmp_path)
    row = log.append(1, datetime.now(UTC), positive_probability=0.9, decision_threshold=0.5)
    source.submit_outcome(
        row.observation_id,
        OutcomeSubmission(
            actual=1, note="explicit scripted fixture", verified=True, kind="SIMULATED"
        ),
        "test-script",
    )
    r = assess_fraud(source, frames(source)[0], policy())
    assert r.status == "WAITING_FOR_LABELS" and r.metrics.samples == 0
    assert r.excluded_simulated_outcomes == 1
