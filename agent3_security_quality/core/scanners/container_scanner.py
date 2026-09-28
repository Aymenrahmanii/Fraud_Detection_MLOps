"""Wraps Trivy for container image vulnerability scanning."""

import json
import shutil
import subprocess

from agent3_security_quality.core.errors import ScannerExecutionError, ScannerUnavailableError
from agent3_security_quality.core.schemas import SecurityResults

_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM")


def run(image_ref: str) -> SecurityResults:
    if shutil.which("trivy") is None:
        raise ScannerUnavailableError("trivy is not installed")

    result = subprocess.run(
        ["trivy", "image", "--format", "json", "--quiet", image_ref],
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ScannerExecutionError(
            f"could not parse trivy output:\n{result.stdout}\n{result.stderr}"
        ) from exc

    counts = {sev: 0 for sev in _SEVERITIES}
    highest_cvss = 0.0

    for scan_target in data.get("Results", []) or []:
        for vuln in scan_target.get("Vulnerabilities", []) or []:
            severity = (vuln.get("Severity") or "").upper()
            if severity in counts:
                counts[severity] += 1
            cvss = vuln.get("CVSS", {}) or {}
            for source_scores in cvss.values():
                score = (source_scores or {}).get("V3Score", 0.0) or 0.0
                highest_cvss = max(highest_cvss, score)

    return SecurityResults(
        critical=counts["CRITICAL"],
        high=counts["HIGH"],
        medium=counts["MEDIUM"],
        secrets_found=0,
        highest_cvss=highest_cvss,
        policy_exception_required=False,
    )
