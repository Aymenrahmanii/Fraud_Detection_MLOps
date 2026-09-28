"""Tests for the optional LLM rationale layer.

All mocked -- CI has no GROQ_API_KEY by default, and even when it does,
a unit test suite should not depend on a live network call to a paid
(even if free-tier) external API. The real integration was verified
manually against the live Groq API before this was wired in; see the
commit message for that record.
"""

from agent3_security_quality.core import rationale
from agent3_security_quality.core.schemas import (
    ArtifactRef,
    QualityResults,
    ReleaseAssessment,
    SecurityResults,
    TestResults,
)


def _assessment():
    return ReleaseAssessment(
        assessment_id="REL-TEST",
        commit="deadbeef",
        artifact=ArtifactRef(image=""),
        tests=TestResults(total=1, passed=1, failed=0, skipped=0, coverage_pct=100.0),
        quality=QualityResults(gate="PASSED", new_critical_issues=0, duplication_pct=0.0),
        security=SecurityResults(),
        decision="ALLOW",
        blocking_reasons=[],
        warnings=[],
    )


def test_returns_none_without_an_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert rationale.generate_rationale(_assessment()) is None


def test_returns_the_model_text_on_success(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    class FakeMessage:
        content = "All checks passed; safe to release."

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeCompletion()

    class FakeChat:
        completions = FakeCompletions()

    class FakeGroqClient:
        def __init__(self, api_key):
            self.chat = FakeChat()

    monkeypatch.setattr("groq.Groq", FakeGroqClient)

    result = rationale.generate_rationale(_assessment())

    assert result == "All checks passed; safe to release."


def test_never_raises_on_api_failure(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    class ExplodingClient:
        def __init__(self, api_key):
            raise RuntimeError("network error, rate limit, whatever")

    monkeypatch.setattr("groq.Groq", ExplodingClient)

    # Must degrade to None, never propagate -- an optional enrichment
    # step must not be able to fail the whole release assessment.
    assert rationale.generate_rationale(_assessment()) is None


def test_never_suggests_a_different_decision_in_the_prompt():
    # The system prompt is the only place decision-tampering could be
    # invited from our side; make sure it explicitly forbids it and
    # forbids inventing findings.
    assert "never suggest a different decision" in rationale._SYSTEM_PROMPT
    assert "never invent findings" in rationale._SYSTEM_PROMPT
