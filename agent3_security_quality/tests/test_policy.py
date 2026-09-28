from agent3_security_quality.core.policy import decide
from agent3_security_quality.core.schemas import (
    QualityResults,
    SecurityResults,
    TestResults,
)


def _tests(**overrides):
    base = dict(total=10, passed=10, failed=0, skipped=0, coverage_pct=90.0, flaky_detected=0)
    base.update(overrides)
    return TestResults(**base)


def _quality(**overrides):
    base = dict(gate="PASSED", new_critical_issues=0, duplication_pct=1.0)
    base.update(overrides)
    return QualityResults(**base)


def _security(**overrides):
    base = dict(critical=0, high=0, medium=0, secrets_found=0, highest_cvss=0.0, policy_exception_required=False)
    base.update(overrides)
    return SecurityResults(**base)


def test_all_clean_allows():
    decision, blocking, warnings = decide(_tests(), _quality(), _security())
    assert decision == "ALLOW"
    assert blocking == []
    assert warnings == []


def test_failing_test_blocks():
    decision, blocking, warnings = decide(_tests(failed=1, passed=9), _quality(), _security())
    assert decision == "BLOCK"
    assert "1 test(s) failing" in blocking


def test_quality_gate_failed_blocks():
    decision, blocking, _ = decide(_tests(), _quality(gate="FAILED"), _security())
    assert decision == "BLOCK"
    assert "quality gate FAILED" in blocking


def test_critical_vulnerability_blocks():
    decision, blocking, _ = decide(_tests(), _quality(), _security(critical=1))
    assert decision == "BLOCK"
    assert "1 critical vulnerability(ies)" in blocking


def test_secret_found_blocks():
    decision, blocking, _ = decide(_tests(), _quality(), _security(secrets_found=1))
    assert decision == "BLOCK"
    assert "1 secret(s) found in scan" in blocking


def test_multiple_blocking_reasons_all_collected():
    decision, blocking, _ = decide(
        _tests(failed=2, passed=8),
        _quality(gate="FAILED"),
        _security(critical=1, secrets_found=1),
    )
    assert decision == "BLOCK"
    assert len(blocking) == 4


def test_high_severity_without_exception_warns():
    decision, blocking, warnings = decide(_tests(), _quality(), _security(high=1))
    assert decision == "ALLOW_WITH_WARNING"
    assert blocking == []
    assert any("high-severity" in w for w in warnings)


def test_high_severity_with_policy_exception_no_warning():
    decision, _, warnings = decide(
        _tests(), _quality(), _security(high=1, policy_exception_required=True)
    )
    # exception already granted -> this specific warning should not fire
    assert not any("high-severity" in w for w in warnings)
    assert decision == "ALLOW"


def test_low_coverage_warns():
    decision, blocking, warnings = decide(_tests(coverage_pct=50.0), _quality(), _security())
    assert decision == "ALLOW_WITH_WARNING"
    assert blocking == []
    assert any("coverage" in w for w in warnings)


def test_flaky_tests_warn():
    decision, _, warnings = decide(_tests(flaky_detected=2), _quality(), _security())
    assert decision == "ALLOW_WITH_WARNING"
    assert any("flaky" in w for w in warnings)


def test_blocking_takes_priority_over_warnings():
    decision, blocking, warnings = decide(
        _tests(failed=1, coverage_pct=10.0),
        _quality(),
        _security(high=1),
    )
    assert decision == "BLOCK"
    assert blocking != []
    # warnings are not evaluated once a block condition is hit
    assert warnings == []
