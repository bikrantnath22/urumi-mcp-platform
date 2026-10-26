import hashlib
import json
import os
import re
from time import perf_counter

import httpx
from mcp.types import Tool

from app.llm_metrics import rate_headers, rejection_details, retry_seconds

MAX_COMPLETION_TOKENS = 512


class LLMError(Exception):
    def __init__(self, message, status_code=502, retry_after=None, *, provider=None, details=None, request_metrics=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.provider = provider
        self.details = details or {}
        self.request_metrics = request_metrics


def build_tool_catalog(tools: list[Tool]) -> tuple[list[dict], dict[str, str]]:
    definitions = []
    aliases = {}

    for tool in tools:
        name = tool.name
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            name = "tool_" + hashlib.sha256(
                tool.name.encode()
            ).hexdigest()[:40]

        if name in aliases:
            raise LLMError("Tool names could not be mapped uniquely.")

        aliases[name] = tool.name
        definitions.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (tool.description or tool.name)[:220],
                "parameters": {
                    key: value
                    for key, value in tool.inputSchema.items()
                    if key != "title"
                },
            },
        })

    return definitions, aliases


def create_llm_client() -> httpx.AsyncClient:
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        raise LLMError("Groq API key is not configured.", status_code=503)

    return httpx.AsyncClient(
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(30, connect=5),
        trust_env=False,
    )


async def request_completion(
    client: httpx.AsyncClient,
    messages: list[dict],
    definitions: list[dict],
    aliases: dict[str, str],
    *,
    tool_choice: str = "auto",
) -> dict:
    payload = {
        "model": os.getenv("LLM_MODEL", "openai/gpt-oss-20b"),
        "messages": messages,
        "reasoning_effort": "low",
        "include_reasoning": False,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
    }

    if definitions:
        payload.update({
            "tools": definitions,
            "tool_choice": tool_choice,
            "parallel_tool_calls": False,
        })

    metrics = {"input_characters": sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) for item in messages),
               "tool_schema_characters": len(json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))) if definitions else 0,
               "outcome": "success"}
    started = perf_counter()
    try:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload,
        )
    except httpx.TimeoutException:
        raise LLMError("Model request timed out.", status_code=504) from None
    except httpx.RequestError:
        raise LLMError(
            "Model service could not be reached.", status_code=503
        ) from None

    metrics["duration_ms"] = int((perf_counter() - started) * 1000)
    metrics["rate_headers"] = rate_headers(response)
    if response.status_code == 429:
        details = rejection_details(response)
        metrics.update(outcome="rejected", usage={}, limit_reason=details["reason"])
        raise LLMError(
            "Groq temporarily rejected the model request because a provider allowance was reached.",
            status_code=429, retry_after=retry_seconds(response), provider="groq",
            details=details, request_metrics=metrics,
        )

    if response.status_code in {401, 403}:
        raise LLMError(
            "Model credentials or access were rejected.", status_code=503
        )

    if response.status_code != 200:
        raise LLMError("Model service rejected the completion request.")

    try:
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]

        if choice.get("finish_reason") == "length":
            raise ValueError("Incomplete model response.")

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("Invalid response text.")

        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list) or len(raw_calls) > 4:
            raise ValueError("Invalid tool-call list.")

        calls = []
        normalized_calls = []
        seen_ids = set()

        for item in raw_calls:
            call_id = item["id"]
            function = item["function"]
            model_name = function["name"]
            arguments = json.loads(function["arguments"])

            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in seen_ids
                or model_name not in aliases
                or not isinstance(arguments, dict)
            ):
                raise ValueError("Invalid tool call.")

            seen_ids.add(call_id)
            calls.append({
                "id": call_id,
                "name": aliases[model_name],
                "arguments": arguments,
            })
            normalized_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": model_name,
                    "arguments": json.dumps(arguments, separators=(",", ":")),
                },
            })

        if not calls and not (content or "").strip():
            raise ValueError("Empty model response.")

        assistant = {"role": "assistant", "content": content}
        if normalized_calls:
            assistant["tool_calls"] = normalized_calls

        return {
            "message": assistant,
            "calls": calls,
            "model": data.get("model", payload["model"]),
            "usage": data.get("usage") or {},
            "request_metrics": metrics,
            "duration_ms": int((perf_counter() - started) * 1000),
        }
    except (KeyError, IndexError, TypeError, ValueError):
        raise LLMError("Model returned an invalid or incomplete response.") from None
