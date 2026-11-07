"""Wraps pip-audit for dependency CVE scanning.

Known limitation (documented, not hidden): pip-audit's JSON output does
not include a CVSS/severity score by default, so this MVP conservatively
counts every known vulnerability as "high" rather than silently treating
unscored findings as harmless. Wiring in real severity (e.g. via OSV.dev
scores) is a Phase 3 improvement, not a blocker for the gate to exist.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

from agent3_security_quality.core.errors import ScannerExecutionError, ScannerUnavailableError
from agent3_security_quality.core.schemas import Finding, SecurityResults


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

    try:
        req_lines = Path(requirements_path).read_text().splitlines()
    except OSError:
        req_lines = []

    def _req_line(package: str) -> int | None:
        for number, text in enumerate(req_lines, start=1):
            name = re.split(r"[=<>~!\[; ]", text.strip(), maxsplit=1)[0]
            if name.lower() == package.lower():
                return number
        return None

    findings = [
        Finding(
            scanner="pip-audit",
            rule=vuln.get("id", ""),
            message=(
                f"{dep.get('name')} {dep.get('version')}: fix in "
                f"{', '.join(vuln.get('fix_versions') or []) or 'no fix available'}"
            ),
            file=requirements_path,
            line=_req_line(dep.get("name", "")),
            severity="high",
        )
        for dep in dependencies
        for vuln in dep.get("vulns", [])
    ]

    return SecurityResults(
        critical=0,
        high=vuln_count,
        medium=0,
        secrets_found=0,
        highest_cvss=0.0,
        policy_exception_required=False,
        findings=findings,
    )
