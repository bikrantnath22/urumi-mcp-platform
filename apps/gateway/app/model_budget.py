"""Per-message model budget; estimates are separate from provider quotas.

UTF-8 size is a tokenizer-free estimate, not an exact token count or price.
Every model round reserves input plus its maximum output before HTTP.
Complete reported usage replaces the reservation after a successful call.
Missing or incomplete usage retains the estimate to keep the loop bounded.
"""
import json
import os

from app.llm import LLMError, MAX_COMPLETION_TOKENS


DEFAULT_TOKEN_BUDGET = 12000


class ModelBudget:
    def __init__(self, limit=None):
        if limit is None:
            raw = os.getenv("CHAT_MODEL_TOKEN_BUDGET", str(DEFAULT_TOKEN_BUDGET))
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 7:
                raise LLMError("Chat model budget is configured incorrectly.", 503)
            limit = int(raw)
        if type(limit) is not int or not 1000 <= limit <= 1000000:
            raise LLMError("Chat model budget must be between 1000 and 1000000.", 503)
        self.limit = limit
        self.charged = 0

    @staticmethod
    def estimate(messages, definitions):
        payload = {"messages": messages, "tools": definitions}
        size = len(json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        return (size + 2) // 3 + 256 + 16 * len(messages) + MAX_COMPLETION_TOKENS

    def reserve(self, messages, definitions):
        requested = self.estimate(messages, definitions)
        if self.charged + requested > self.limit:
            raise LLMError(
                "This message reached its model-processing budget. "
                "Check saved tool activity before asking for remaining steps; "
                "do not repeat completed actions.",
                status_code=429,
                details={
                    "reason": "chat_model_budget",
                    "limit": self.limit,
                    "used": self.charged,
                    "requested": requested,
                    "estimated": True,
                },
            )
        self.charged += requested
        return requested

    def record(self, reserved, usage):
        if not isinstance(usage, dict):
            return
        values = [
            value for key in ("total_tokens",)
            if type(value := usage.get(key)) is int and value >= 0
        ]
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        output = usage.get("completion_tokens", usage.get("output_tokens"))
        if (
            type(prompt) is int and prompt >= 0
            and type(output) is int and output >= 0
        ):
            values.append(prompt + output)
        if values:
            # Replace this request's reservation with reported usage.
            self.charged += max(values) - reserved
