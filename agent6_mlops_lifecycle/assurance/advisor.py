"""Optional Groq explanation of aggregates; it cannot write labels or change decisions."""

import json
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Environment, StrictUndefined

from agent6_mlops_lifecycle.assurance.interpretation import eligible_checks, render_plan
from agent6_mlops_lifecycle.assurance.models import AdvisorExplanation, AdvisoryPlan
from agent6_mlops_lifecycle.assurance.store import encode
from agent6_mlops_lifecycle.evaluation.llm_judge import LLMJudgeSettings
from agent6_mlops_lifecycle.monitoring.settings import MonitoringSettings

PROMPT_VERSION = "monitoring-advisor-v4"
_TEMPLATE = Path(__file__).parent / "prompts/monitoring-advisor.jinja2"


async def explain(
    settings: MonitoringSettings,
    evidence: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AdvisorExplanation:
    key = LLMJudgeSettings().api_key_for("groq")
    payload = encode(evidence).decode()
    if len(payload) > 48000:
        raise ValueError("Advisor evidence exceeds its disclosure bound")
    template = Environment(undefined=StrictUndefined, autoescape=False).from_string(
        _TEMPLATE.read_text(encoding="utf-8")
    )
    prompt = template.render(
        prompt_version=PROMPT_VERSION,
        evidence_json=payload,
        eligible_json=json.dumps(eligible_checks(evidence)),
    )
    body = {
        "model": settings.advisor_model,
        "messages": [
            {
                "role": "system",
                "content": "You explain monitoring evidence; you never make operational decisions.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_completion_tokens": 4096,
        "reasoning_effort": "low",
        "include_reasoning": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "monitoring_explanation",
                "strict": True,
                "schema": AdvisoryPlan.model_json_schema(),
            },
        },
    }
    try:
        async with (
            httpx.AsyncClient(timeout=30, trust_env=False, transport=transport) as client,
            client.stream(
                "POST",
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
            ) as response,
        ):
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 128000:
                    raise ValueError("Advisor response exceeds its size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid advisor response envelope")
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("Invalid advisor choices")
        message = choices[0].get("message")
        if (
            choices[0].get("finish_reason") != "stop"
            or not isinstance(message, dict)
            or message.get("refusal")
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("Incomplete advisor response")
        plan = AdvisoryPlan.model_validate_json(message["content"])
        return render_plan(evidence, plan)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as error:
        raise RuntimeError(
            "Advisor unavailable or returned invalid evidence references; "
            "measured monitoring continues"
        ) from error
