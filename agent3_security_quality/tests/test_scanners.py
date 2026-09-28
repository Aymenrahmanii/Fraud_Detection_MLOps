import json
import subprocess

import pytest

from agent3_security_quality.core import errors
from agent3_security_quality.core.scanners import (
    container_scanner,
    dependency_scanner,
    quality_scanner,
    secrets_scanner,
    test_runner,
)


# ---------------------------------------------------------------------------
# Real-tool tests (pytest/ruff/pip-audit are installed in this environment)
# ---------------------------------------------------------------------------


def test_test_runner_parses_a_passing_suite(tmp_path):
    sample = tmp_path / "test_sample.py"
    sample.write_text("def test_ok():\n    assert 1 == 1\n")

    result = test_runner.run(str(sample), junit_path=str(tmp_path / "junit.xml"))

    assert result.total == 1
    assert result.passed == 1
    assert result.failed == 0


def test_test_runner_parses_a_failing_suite(tmp_path):
    sample = tmp_path / "test_sample.py"
    sample.write_text("def test_fail():\n    assert 1 == 2\n")

    result = test_runner.run(str(sample), junit_path=str(tmp_path / "junit.xml"))

    assert result.total == 1
    assert result.failed == 1
    assert result.passed == 0


def test_quality_scanner_flags_undefined_name(tmp_path):
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("def f():\n    return undefined_name\n")

    result = quality_scanner.run(str(bad_file))

    assert result.gate == "FAILED"
    assert result.new_critical_issues >= 1


def test_quality_scanner_passes_clean_file(tmp_path):
    good_file = tmp_path / "good.py"
    good_file.write_text("def f() -> int:\n    return 1\n")

    result = quality_scanner.run(str(good_file))

    assert result.gate == "PASSED"
    assert result.new_critical_issues == 0


def test_quality_scanner_does_not_treat_simplify_suggestions_as_critical(tmp_path):
    # Regression test: ruff's flake8-simplify codes ("SIM102") start with
    # the same letter as its bandit-security codes ("S105"), but they are
    # style suggestions, not security findings, and must not fail the gate.
    nested_if_file = tmp_path / "nested_if.py"
    nested_if_file.write_text(
        "def f(a, b):\n"
        "    if a:\n"
        "        if b:\n"
        "            return True\n"
        "    return False\n"
    )

    result = quality_scanner.run(str(nested_if_file))

    assert result.gate == "PASSED"
    assert result.new_critical_issues == 0


def test_quality_scanner_flags_real_bandit_security_code(tmp_path):
    hardcoded_password_file = tmp_path / "insecure.py"
    hardcoded_password_file.write_text('password = "hunter2"\n')

    result = quality_scanner.run(str(hardcoded_password_file))

    assert result.gate == "FAILED"
    assert result.new_critical_issues >= 1


def test_dependency_scanner_handles_clean_requirements(tmp_path):
    # A tiny, presumably-unvulnerable pin -- this just proves the scanner
    # runs and parses output; it is not asserting a specific vuln count
    # (that would break the moment upstream advisories change).
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("pip\n")

    result = dependency_scanner.run(str(req_file))

    assert result.high >= 0  # ran and returned structured data, didn't crash


# ---------------------------------------------------------------------------
# Mocked-subprocess tests for tools not installed in this sandbox
# (gitleaks, trivy) -- CI installs the real binaries; these tests only
# prove our parsing/plumbing logic is correct.
# ---------------------------------------------------------------------------


def test_secrets_scanner_reports_zero_when_no_findings(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gitleaks")

    report_path = tmp_path / "gitleaks-report.json"

    def fake_run(cmd, capture_output, text):
        # gitleaks writes an empty file when nothing is found
        report_path.write_text("")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = secrets_scanner.run(source=str(tmp_path), report_path=str(report_path))

    assert result.secrets_found == 0


def test_secrets_scanner_counts_findings(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gitleaks")

    report_path = tmp_path / "gitleaks-report.json"
    findings = [{"RuleID": "generic-api-key", "File": "config.py"}]

    def fake_run(cmd, capture_output, text):
        report_path.write_text(json.dumps(findings))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = secrets_scanner.run(source=str(tmp_path), report_path=str(report_path))

    assert result.secrets_found == 1


def test_secrets_scanner_raises_when_gitleaks_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(errors.ScannerUnavailableError):
        secrets_scanner.run()


def test_container_scanner_counts_by_severity(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/trivy")

    fake_output = {
        "Results": [
            {
                "Vulnerabilities": [
                    {"Severity": "CRITICAL", "CVSS": {"nvd": {"V3Score": 9.8}}},
                    {"Severity": "HIGH", "CVSS": {"nvd": {"V3Score": 7.5}}},
                    {"Severity": "LOW", "CVSS": {}},
                ]
            }
        ]
    }

    def fake_run(cmd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 0, json.dumps(fake_output), "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = container_scanner.run("example/image:latest")

    assert result.critical == 1
    assert result.high == 1
    assert result.highest_cvss == 9.8


def test_container_scanner_raises_when_trivy_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(errors.ScannerUnavailableError):
        container_scanner.run("example/image:latest")


def test_container_scanner_raises_on_bad_json(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/trivy")

    def fake_run(cmd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 0, "not json", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(errors.ScannerExecutionError):
        container_scanner.run("example/image:latest")
