"""Wraps ruff as the code-quality gate.

There is no SonarQube server in this project (yet), so duplication_pct
stays at 0.0 for now -- a real duplication/maintainability tool is a
Phase 3+ addition, not something to fake here.
"""

import json
import shutil
import subprocess

from agent3_security_quality.core.errors import ScannerExecutionError, ScannerUnavailableError
from agent3_security_quality.core.schemas import Finding, QualityResults

# The rule categories that fail the gate. This *is* the quality policy --
# extend it deliberately, not by accident.
#
# IMPORTANT: ruff's bandit-security rules ("S", e.g. S105 hardcoded
# password) are NOT part of ruff's default rule set -- they must be
# explicitly selected, unlike pyflakes ("F") and pycodestyle ("E") which
# ruff runs by default. Relying on plain `ruff check` with no --select
# and then filtering the output client-side silently misses every real
# security finding while still picking up unrelated categories that
# happen to share a letter prefix (e.g. flake8-simplify's "SIM1xx" also
# starts with "S"). Asking ruff for exactly these codes avoids both
# problems at once.
# F821 : undefined-name : Utilisation d'une variable ou d'une fonction non définie. 
# F811 : Sécurité (flake8-bandit) : Réutilisation du nom d'une variable ou d'une fonction déjà déclarée.
# E9 : SyntaxError / IndentationError: Erreur de syntaxe ou erreur critique.
# S : Security : Règles de sécurité (par exemple, S105 pour les mots de passe en clair).
#print(utilisateur_id)  # F821: Le nom 'utilisateur_id' n'est pas défini
#import os  # F811: Importé mais jamais utilisé car redéfini ci-dessous


CRITICAL_RULE_CODES = ("F821", "F811", "E9", "S")


def run(*targets: str) -> QualityResults:
    if shutil.which("ruff") is None:
        raise ScannerUnavailableError("ruff is not installed")

    result = subprocess.run(
        ["ruff", "check", *targets, "--select", ",".join(CRITICAL_RULE_CODES), "--output-format=json"],
        capture_output=True,
        text=True,
    )
    # ruff exits 1 when it finds issues -- that's expected, not a scanner failure.
    try:
        critical = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ScannerExecutionError(
            f"could not parse ruff output:\n{result.stdout}\n{result.stderr}"
        ) from exc

    return QualityResults(
        gate="FAILED" if critical else "PASSED",
        new_critical_issues=len(critical),
        duplication_pct=0.0,  # TODO(phase 3): wire in a real duplication tool
        findings=[
            Finding(
                scanner="ruff",
                rule=i.get("code") or "",
                message=i.get("message", ""),
                file=i.get("filename", ""),
                line=(i.get("location") or {}).get("row"),
                severity="critical",
            )
            for i in critical
        ],
    )
