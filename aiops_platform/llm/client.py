"""LLM client for Agent 2's investigation: a Gemini REST client and the types it returns.

Gemini 3 models sign their function calls: the model turn must be sent back
exactly as the API returned it, so `Reply.raw` carries that turn unchanged.
The API key goes in a request header, never in a URL, so it cannot end up in
logs that print URLs.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    """The model could not be reached or did not answer. The message never carries the prompt or the key."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Reply:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    raw: dict[str, Any]  # the model turn as returned; sent back unchanged in the next request


class GeminiClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self.model_name = model
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={"x-goog-api-key": api_key, "User-Agent": "aiops-platform/1.0"},
        )

    async def generate(self, *, system: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        try:
            response = await self._http.post(GEMINI_ENDPOINT.format(model=self.model_name), json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"transport error: {type(exc).__name__}") from exc
        if response.status_code != 200:
            # The body can echo the request, so only the status code is reported.
            raise LLMError(f"HTTP {response.status_code}")
        try:
            content = response.json()["candidates"][0]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("response had no content (blocked or empty)") from exc
        parts = content.get("parts") or []
        calls = tuple(
            ToolCall(name=part["functionCall"]["name"], args=dict(part["functionCall"].get("args") or {}))
            for part in parts
            if "functionCall" in part
        )
        text = "".join(part.get("text", "") for part in parts if "text" in part and not part.get("thought"))
        return Reply(text=text or None, tool_calls=calls, raw={"role": "model", "parts": parts})

    async def aclose(self) -> None:
        await self._http.aclose()
