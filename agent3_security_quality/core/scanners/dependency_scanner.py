"""Wraps pip-audit for dependency CVE scanning.

Known limitation (documented, not hidden): pip-audit's JSON output does
not include a CVSS/severity score by default, so this MVP conservatively
counts every known vulnerability as "high" rather than silently treating
unscored findings as harmless. Wiring in real severity (e.g. via OSV.dev
scores) is a Phase 3 improvement, not a blocker for the gate to exist.
"""

import json
import shutil
import subprocess

from agent3_security_quality.core.errors import ScannerExecutionError, ScannerUnavailableError
from agent3_security_quality.core.schemas import SecurityResults


def run(requirements_path: str = "requirements.txt") -> SecurityResults:
    if shutil.which("pip-audit") is None:
        raise ScannerUnavailableError("pip-audit is not installed")

    result = subprocess.run(
        ["pip-audit", "-r", requirements_path, "--format", "json"],
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ScannerExecutionError(
            f"could not parse pip-audit output:\n{result.stdout}\n{result.stderr}"
        ) from exc

    # pip-audit >=2.6 wraps results in {"dependencies": [...]}; older
    # versions emit a bare list. Support both.
    dependencies = data.get("dependencies", []) if isinstance(data, dict) else data
    vuln_count = sum(len(dep.get("vulns", [])) for dep in dependencies)

    return SecurityResults(
        critical=0,
        high=vuln_count,
        medium=0,
        secrets_found=0,
        highest_cvss=0.0,
        policy_exception_required=False,
    )
