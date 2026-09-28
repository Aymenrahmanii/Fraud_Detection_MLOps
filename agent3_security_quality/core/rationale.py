"""Optional LLM rationale layer.

Turns the already-finalized scan results into a short, human-readable
summary. This runs strictly *after* policy.decide() and never influences
decision/blocking_reasons/warnings -- consistent with the architecture
spec's "LLM outputs are untrusted suggestions" rule. If no API key is
configured, the SDK isn't installed, or the call fails for any reason,
this degrades to None rather than failing the whole assessment -- an
optional enrichment step must never be able to block a release gate.

Uses Groq (not Anthropic) by explicit choice: this task is small
input/output text classification-adjacent work, and Groq has a genuine
free tier, unlike a standing Anthropic API key.
"""

import os

from agent3_security_quality.core.schemas import ReleaseAssessment

DEFAULT_MODEL = "openai/gpt-oss-20b"

_SYSTEM_PROMPT = (
    "You are a terse release-notes assistant for a software security/quality "
    "release gate. Given structured scan results as JSON-ish key: value pairs, "
    "write a plain-English summary a developer can read at a glance. Hard "
    "limit: 40 words, at most 2 sentences -- stop well before that limit "
    "rather than write a partial final sentence. State only facts present in "
    "the data -- never invent findings, and never suggest a different "
    "decision than the one given; the decision is already final."
)


def generate_rationale(assessment: ReleaseAssessment, *, model: str = DEFAULT_MODEL) -> str | None:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None

    try:
        from groq import Groq
    except ImportError:
        return None

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"decision: {assessment.decision}\n"
                        f"blocking_reasons: {assessment.blocking_reasons}\n"
                        f"warnings: {assessment.warnings}\n"
                        f"tests: {assessment.tests.model_dump()}\n"
                        f"quality: {assessment.quality.model_dump()}\n"
                        f"security: {assessment.security.model_dump()}"
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=300,
            # openai/gpt-oss-20b is a reasoning model: without this, it can
            # spend its entire max_tokens budget on hidden chain-of-thought
            # (observed: 298/300 tokens) and return empty content with
            # finish_reason="length". This task doesn't need deep reasoning.
            reasoning_effort="low",
            timeout=15,
        )
        text = response.choices[0].message.content
        return text.strip() if text else None
    except Exception:  # noqa: BLE001 -- intentional: see module docstring
        return None
