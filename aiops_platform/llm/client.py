"""LLM clients for Agent 2's investigation: Gemini and Groq, behind one interface.

The investigator keeps one internal conversation format, the Gemini shape
(`contents` with text, functionCall and functionResponse parts). Each client
converts that format to its provider's request and converts the answer back.

Gemini 3 models sign their function calls: the model turn must be sent back
exactly as the API returned it, so `Reply.raw` carries that turn unchanged.
Keys go in request headers, never in URLs, so they cannot end up in logs that
print URLs.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
MAX_RATE_LIMIT_RETRIES = 3


class LLMError(Exception):
    """The model could not be reached or did not answer. The message never carries the prompt or the key."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None  # the provider's call id, when it has one (Groq does; Gemini may)


@dataclass(frozen=True)
class Reply:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    raw: dict[str, Any]  # the model turn in the internal format; sent back unchanged in the next request


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
            ToolCall(
                name=part["functionCall"]["name"],
                args=dict(part["functionCall"].get("args") or {}),
                id=part["functionCall"].get("id"),
            )
            for part in parts
            if "functionCall" in part
        )
        text = "".join(part.get("text", "") for part in parts if "text" in part and not part.get("thought"))
        return Reply(text=text or None, tool_calls=calls, raw={"role": "model", "parts": parts})

    async def aclose(self) -> None:
        await self._http.aclose()


def _lower_schema(schema: Any) -> Any:
    """Gemini writes schema types in capitals (OBJECT, STRING); the OpenAI format uses lowercase."""
    if isinstance(schema, dict):
        return {
            key: (value.lower() if key == "type" and isinstance(value, str) else _lower_schema(value))
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [_lower_schema(item) for item in schema]
    return schema


def to_openai_tools(declarations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini function declarations, as Groq's OpenAI-compatible API expects them."""
    return [
        {
            "type": "function",
            "function": {
                "name": decl["name"],
                "description": decl.get("description", ""),
                "parameters": _lower_schema(decl.get("parameters") or {"type": "OBJECT", "properties": {}}),
            },
        }
        for decl in declarations
    ]


def to_openai_messages(system: str, contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The internal conversation as chat messages. Tool results are matched to their calls by id."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in contents:
        parts = turn.get("parts") or []
        if turn.get("role") == "model":
            text = "".join(part["text"] for part in parts if "text" in part)
            calls = [part["functionCall"] for part in parts if "functionCall" in part]
            message: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call.get("id") or f"call_{index}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call.get("args") or {})},
                    }
                    for index, call in enumerate(calls)
                ]
            messages.append(message)
            continue
        responses = [part["functionResponse"] for part in parts if "functionResponse" in part]
        if responses:
            for index, response in enumerate(responses):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": response.get("id") or f"call_{index}",
                        "content": json.dumps(response.get("response", {}), default=str),
                    }
                )
        else:
            messages.append({"role": "user", "content": "".join(part.get("text", "") for part in parts)})
    return messages


class GroqClient:
    """Groq's OpenAI-compatible chat API. Groq rejects requests without a User-Agent header."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set")
        self.model_name = model
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "aiops-platform/1.0"},
        )

    async def generate(self, *, system: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": to_openai_messages(system, contents),
            "temperature": 0,
            "max_completion_tokens": 1024,
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
            body["tool_choice"] = "auto"
        response = await self._post_with_rate_limit_retries(body)
        if response.status_code != 200:
            raise LLMError(f"HTTP {response.status_code}")
        try:
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("response had no message") from exc
        calls = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(
                ToolCall(
                    name=str(function.get("name", "")),
                    args=args if isinstance(args, dict) else {},
                    id=raw_call.get("id"),
                )
            )
        content = message.get("content")
        parts: list[dict[str, Any]] = []
        if content:
            parts.append({"text": content})
        parts += [{"functionCall": {"name": c.name, "args": c.args, "id": c.id}} for c in calls]
        return Reply(text=content or None, tool_calls=tuple(calls), raw={"role": "model", "parts": parts})

    async def _post_with_rate_limit_retries(self, body: dict[str, Any]) -> httpx.Response:
        """Waits out a rate limit (HTTP 429) as the provider asks, a few times, before giving up.
        The waiting counts toward the investigation's time."""
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._http.post(GROQ_ENDPOINT, json=body)
            except httpx.HTTPError as exc:
                raise LLMError(f"transport error: {type(exc).__name__}") from exc
            if response.status_code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                return response
            try:
                wait = float(response.headers.get("retry-after", "10"))
            except ValueError:
                wait = 10.0
            await asyncio.sleep(min(max(wait, 1.0), 30.0))
        return response

    async def aclose(self) -> None:
        await self._http.aclose()
