"""Tests for the MCP tool wrappers.

These mock run_assessment itself -- the real scanners are already
covered by test_scanners.py and test_policy.py, and re-running the full
(network-bound, multi-minute) scan here would make this suite slow for
no extra coverage.
"""

import asyncio
import json

from agent3_security_quality import mcp_server
from agent3_security_quality.core.schemas import (
    ArtifactRef,
    QualityResults,
    ReleaseAssessment,
    SecurityResults,
    TestResults,
)


def _fake_assessment(decision="ALLOW"):
    return ReleaseAssessment(
        assessment_id="REL-TEST",
        commit="deadbeef",
        artifact=ArtifactRef(image=""),
        tests=TestResults(total=1, passed=1, failed=0, skipped=0, coverage_pct=100.0),
        quality=QualityResults(gate="PASSED", new_critical_issues=0, duplication_pct=0.0),
        security=SecurityResults(),
        decision=decision,
        blocking_reasons=[],
        warnings=[],
    )


def test_run_release_assessment_returns_the_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(mcp_server, "run_assessment", lambda **kwargs: _fake_assessment())

    result = mcp_server.run_release_assessment(image="", strict=False)

    assert result["decision"] == "ALLOW"
    assert result["assessment_id"] == "REL-TEST"


def test_run_release_assessment_writes_the_cache_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(mcp_server, "_CACHE_PATH", cache_path)
    monkeypatch.setattr(mcp_server, "run_assessment", lambda **kwargs: _fake_assessment("BLOCK"))

    mcp_server.run_release_assessment()

    assert cache_path.exists()
    assert json.loads(cache_path.read_text())["decision"] == "BLOCK"


def test_run_release_assessment_forwards_arguments(tmp_path, monkeypatch):
    captured = {}

    def fake_run_assessment(**kwargs):
        captured.update(kwargs)
        return _fake_assessment()

    monkeypatch.setattr(mcp_server, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(mcp_server, "run_assessment", fake_run_assessment)

    mcp_server.run_release_assessment(image="fraud-api:ci", strict=True, rationale=True)

    assert captured == {"image_ref": "fraud-api:ci", "strict": True, "with_rationale": True}


def test_get_latest_assessment_without_a_prior_run_reports_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_CACHE_PATH", tmp_path / "does-not-exist.json")

    result = mcp_server.get_latest_assessment()

    assert "error" in result


def test_get_latest_assessment_returns_the_cached_result(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(_fake_assessment().model_dump()))
    monkeypatch.setattr(mcp_server, "_CACHE_PATH", cache_path)

    result = mcp_server.get_latest_assessment()

    assert result["assessment_id"] == "REL-TEST"


def test_tools_are_registered_with_the_mcp_server():
    tools = asyncio.run(mcp_server.server.list_tools())
    tool_names = {t.name for t in tools}

    assert tool_names == {"run_release_assessment", "get_latest_assessment"}


def test_get_latest_assessment_reachable_through_call_tool(tmp_path, monkeypatch):
    # Proves the MCP protocol layer itself works, not just the plain
    # Python function -- call_tool is what an actual MCP client uses.
    monkeypatch.setattr(mcp_server, "_CACHE_PATH", tmp_path / "does-not-exist.json")

    result = asyncio.run(mcp_server.server.call_tool("get_latest_assessment", {}))

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert "error" in payload
