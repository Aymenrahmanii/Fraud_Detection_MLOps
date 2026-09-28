"""MCP server wrapper for Agent 3 (Security & Quality).

Exposes the same deterministic release gate used by the CLI and CI
workflow as MCP tools, so an Orchestrator or a Claude Code session can
query it directly instead of only through a CI job's exit code.

Two tools only, deliberately:
  - run_release_assessment: runs the real scan and returns the result.
  - get_latest_assessment: returns the last cached result without
    re-running anything.

No `get_scan_evidence`-style tool yet, even though the original plan
sketched one -- that would need a real evidence store (the Context
Service from the six-agent architecture), which doesn't exist yet.
Building a fake one here would just be scaffolding nothing points at.

Known limitation: run_release_assessment can take a couple of minutes
(pip-audit resolves the full dependency tree against OSV over the
network). MCP clients that impose a short tool-call timeout may need
that raised for this tool specifically.
"""

import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from agent3_security_quality.core.assess import run_assessment

server = MCPServer(
    "agent3-security-quality",
    instructions=(
        "Runs the fraud-detection service's release gate: tests, code "
        "quality (ruff), dependency CVEs (pip-audit), committed secrets "
        "(gitleaks), and container vulnerabilities (Trivy, only when an "
        "image ref is given). Returns a RELEASE_ASSESSMENT payload with a "
        "BLOCK / ALLOW_WITH_WARNING / ALLOW decision. Scan results are "
        "authoritative; this server does no LLM reasoning of its own."
    ),
)

_CACHE_PATH = Path(".agent3_last_assessment.json")


@server.tool()
def run_release_assessment(image: str = "", strict: bool = False) -> dict:
    """Run the full release-gate scan now and return the result.

    Slow (often 1-3 minutes) because of network-bound dependency
    scanning. Use get_latest_assessment for a cached result instead when
    you don't need a fresh run.

    Args:
        image: container image ref to pass to Trivy; skipped if empty.
        strict: treat a missing/failed scanner as BLOCK instead of a
            warning (matches CI behavior; local/dev runs normally leave
            this False).
    """
    assessment = run_assessment(image_ref=image, strict=strict)
    payload = assessment.model_dump()
    _CACHE_PATH.write_text(json.dumps(payload, indent=2))
    return payload


@server.tool()
def get_latest_assessment() -> dict:
    """Return the most recent release assessment without re-running scans.

    Returns an object with an "error" key if run_release_assessment
    hasn't been called yet in this environment.
    """
    if not _CACHE_PATH.exists():
        return {"error": "no assessment has been run yet; call run_release_assessment first"}
    return json.loads(_CACHE_PATH.read_text())


if __name__ == "__main__":
    server.run()
