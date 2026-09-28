"""Wraps gitleaks for committed-secrets detection."""

import json
import shutil
import subprocess
from pathlib import Path

from agent3_security_quality.core.errors import ScannerUnavailableError
from agent3_security_quality.core.schemas import SecurityResults


def run(source: str = ".", report_path: str = "gitleaks-report.json") -> SecurityResults:
    if shutil.which("gitleaks") is None:
        raise ScannerUnavailableError("gitleaks is not installed")

    subprocess.run(
        [
            "gitleaks", "detect",
            "--source", source,
            "--report-format", "json",
            "--report-path", report_path,
            "--exit-code", "0",
            "--no-banner",
        ],
        capture_output=True,
        text=True,
    )

    report_file = Path(report_path)
    findings = []
    if report_file.exists() and report_file.stat().st_size > 0:
        findings = json.loads(report_file.read_text())

    return SecurityResults(
        critical=0,
        high=0,
        medium=0,
        secrets_found=len(findings),
        highest_cvss=0.0,
        policy_exception_required=False,
    )
