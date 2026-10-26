"""Budget and chat-loop checks with mocked model/storage/tool boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1]),
    str(Path(__file__).resolve().parent),
]

import httpx
from mcp.types import Tool
from verify_analytics_chat import (
    model_call, model_reply, provider_limit, scenario,
)
from app import llm
from app.chat_tool_search import MAX_SCHEMA_CHARACTERS, SEARCH_TOOL_NAME
from app.model_budget import ModelBudget
from app.chat_context import previous_context


def tool(name, description):
    return Tool(
        name=name, description=description,
        inputSchema={"type": "object", "properties": {}},
    )


TOOLS = [
    tool("analytics__order_counts", "Count all store orders by status."),
    tool("analytics__sales_summary", "Summarize gross sales by currency."),
    tool("woo__list_products", "List store products with prices."),
    tool("woo__create_product", "Create a product with a name and price."),
    tool("notes__add_note", "Save a team note."),
    tool("notes__get_note", "Read a team note by ID."),
]
TOOLS.extend(
    tool(f"service-{number:03d}__health", "Check service health.")
    for number in range(400)
)
MIXED = "all order count and total sale and list 3 product"


def rejects(operation, status):
    try:
        operation()
    except llm.LLMError as error:
        assert error.status_code == status
        return error
    raise AssertionError("Invalid budget or exhausted budget was accepted")


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


async def check():
    messages = [{"role": "user", "content": "Tea " * 1000}]
    estimate = ModelBudget.estimate(messages, [])
    budget = ModelBudget(estimate)
    reserved = budget.reserve(messages, [])
    assert reserved == estimate and budget.charged == estimate
    budget.record(reserved, {})
    assert budget.charged == estimate
    error = rejects(lambda: budget.reserve(messages, []), 429)
    assert error.details["reason"] == "chat_model_budget"
    assert error.details["estimated"] is True
    assert error.provider is None and error.retry_after is None
    budget.record(reserved, {"total_tokens": estimate + 100})
    assert budget.charged == estimate + 100
    assert ModelBudget(estimate).charged == 0
    rejects(lambda: ModelBudget(True), 503)
    rejects(lambda: ModelBudget(0), 503)
    with patch.dict(os.environ, {"CHAT_MODEL_TOKEN_BUDGET": "NaN"}):
        rejects(ModelBudget, 503)
    assert ModelBudget.estimate(
        [{"role": "user", "content": "茶" * 100}], []
    ) > ModelBudget.estimate(
        [{"role": "user", "content": "x" * 100}], []
    )
    print("Budget reservation, missing usage, reported usage, UTF-8 and configuration checks passed")

    for usage, expected in (
        ({"total_tokens": 140}, 140),
        ({"prompt_tokens": 100, "completion_tokens": 40}, 140),
        ({"input_tokens": 100, "output_tokens": 40}, 140),
        ({"total_tokens": 140, "prompt_tokens": 100, "completion_tokens": 50}, 150),
        ({}, estimate),
        (None, estimate),
        ({"prompt_tokens": 100}, estimate),
        ({"completion_tokens": 40}, estimate),
        ({"total_tokens": True}, estimate),
        ({"total_tokens": -1}, estimate),
        ({"total_tokens": "140"}, estimate),
    ):
        settled = ModelBudget(estimate + 1000)
        reservation = settled.reserve(messages, [])
        settled.record(reservation, usage)
        assert settled.charged == expected, (usage, settled.charged)

    settled = ModelBudget(12000)
    large_input = [{"role": "system", "content": "context " * 1100}]
    for number in range(4):
        reservation = settled.reserve(large_input, [])
        settled.record(reservation, {
            "prompt_tokens": 1600,
            "completion_tokens": 100,
            "total_tokens": 1700,
        })
        assert settled.charged == 1700 * (number + 1)

    rejects(
        lambda: settled.reserve([{"role": "user", "content": "x" * 20000}], []),
        429,
    )
    print("Usage settlement passed; missing usage and real exhaustion stay bounded")

    captured = []

    def provider(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Fixture reply."},
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        })

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider)
    ) as client:
        await llm.request_completion(
            client, [{"role": "user", "content": "Fixture"}], [], {},
        )
    assert captured[0]["max_completion_tokens"] == llm.MAX_COMPLETION_TOKENS
    print("Budget and actual completion payload share the same output allowance")

    turns = []
    with patch.dict(os.environ, {"CHAT_MODEL_TOKEN_BUDGET": "12000"}):
        for number in range(6):
            history = previous_context(turns[:3], MIXED)
            if number:
                assert history, "Repeated requests must exercise recent context"

            steps = [
                model_call("analytics__order_counts", {"date_from": "", "date_to": ""}),
                model_call("analytics__sales_summary", {"date_from": "", "date_to": ""}),
                model_call("woo__list_products", {"per_page": 3}),
                model_reply("Fixture totals and products."),
            ]
            for step in steps:
                step["usage"] = {
                    "prompt_tokens": 1200,
                    "completion_tokens": 60,
                    "total_tokens": 1260,
                }

            repeated = await scenario(
                steps, message=MIXED, available=TOOLS, history=history,
            )
            assert repeated["status"] == 200, repeated["data"]
            assert len(repeated["executions"]) == 3
            assert len(repeated["requests"]) == 4
            assert repeated["requests"][-1][1:3] == ([], {})
            assert repeated["data"]["model_requests"][-1]["budget_charged_tokens"] == 5040

            turns.insert(0, SimpleNamespace(
                message=MIXED, response=repeated["data"], image_id=None,
            ))

    print("Six repeated mixed turns passed with recent context and the 12000 budget")

    # This generous budget exists only inside this mocked verification.
    with patch.dict(os.environ, {"CHAT_MODEL_TOKEN_BUDGET": "100000"}):
        state = await scenario([
            model_call("analytics__order_counts", {"date_from": "", "date_to": ""}),
            model_call("analytics__sales_summary", {"date_from": "", "date_to": ""}),
            model_call("woo__list_products", {"per_page": 3}),
            model_reply("Fixture totals and products."),
        ], message=MIXED, available=TOOLS)
        assert state["status"] == 200, state["data"]
        assert [name for name, args, options in state["executions"]] == [
            "analytics__order_counts",
            "analytics__sales_summary",
            "woo__list_products",
        ]
        for _, definitions, aliases, _ in state["requests"]:
            assert len(aliases) <= 5
            assert encoded_size(definitions) <= MAX_SCHEMA_CHARACTERS
        assert state["requests"][-1][1:3] == ([], {})
        assert all(
            item["budget_reserved_tokens"] > 0
            for item in state["data"]["model_requests"]
        )
        print("Mixed request uses bounded definitions and three gateway actions; final request has no schemas")

        order_tool = "analytics-test2-222222222222__order_counts"
        note_tool = "notes__add_note"
        combined = [
            *TOOLS,
            tool(order_tool, "[test2 / Analytics] Count orders by status and creation date."),
            tool("woo-test2-222222222222__get_order", "[test2 / WooCommerce] Get one order by ID."),
            tool("notes__list_notes", "[Team Notes] List shared team notes."),
            tool("notes__info", "[Team Notes] Describe the notes server."),
        ]
        combined = [item.model_copy(update={
            "description": "[Team Notes] Save a shared team note."
        }) if item.name == note_tool else item for item in combined]
        for query in (note_tool, "test2 order_counts"):
            state = await scenario([
                model_call(SEARCH_TOOL_NAME, {"query": query}),
                model_call(order_tool, {"date_from": "2026-10-25", "date_to": "2026-10-25"}),
                model_call(note_tool, {"title": "test2 daily orders", "body": "2 orders."}),
                model_reply("Count retrieved and note saved."),
            ], message="Get today's order count for test2, then save the result in Team Notes.",
                available=combined, history=[])
            assert state["status"] == 200, state["data"]
            assert [name for name, args, options in state["executions"]] == [order_tool, note_tool]
            assert len(state["data"]["tool_calls"]) == 2
            assert state["data"]["tool_calls"][1]["arguments"]["body"] == "2 orders."
            for _, definitions, aliases, _ in state["requests"]:
                assert len(aliases) <= 5 and encoded_size(definitions) <= MAX_SCHEMA_CHARACTERS
        print("Analytics then Notes completes after either lookup; searches perform no actions or duplicate writes")

        searches = [
            model_call(SEARCH_TOOL_NAME, {"query": "", "offset": offset})
            for offset in (0, 6, 12, 18)
        ]
        searches.append(model_call(SEARCH_TOOL_NAME, {"query": MIXED}))
        state = await scenario([
            *searches,
            model_call("analytics__order_counts", {}),
            model_call("analytics__sales_summary", {}),
            model_call("woo__list_products", {"per_page": 3}),
            model_reply(),
        ], message="Look up the parcel progress", available=TOOLS)
        assert state["status"] == 200, state["data"]
        assert len(state["requests"]) == 9
        assert len(state["executions"]) == 3
        assert len(state["data"]["tool_calls"]) == 3
        assert state["requests"][-1][1:3] == ([], {})
        print("Five searches plus three actions complete; searches consume no action slots")

        state = await scenario([
            model_call(SEARCH_TOOL_NAME, {"query": "get note"}),
            model_call(SEARCH_TOOL_NAME, {"query": "get note"}),
        ], message="Look up the parcel progress", available=TOOLS)
        assert state["status"] == 502 and not state["executions"]
        assert "repeated" in state["data"]["error"]["message"]

        state = await scenario([
            model_call("private__delete_record", {"id": 1}),
        ], message=MIXED, available=TOOLS)
        assert state["status"] == 502 and not state["executions"]
        print("Repeated-search loops and unloaded or ungranted tool names execute nothing")

        arguments = {"name": "Tea", "regular_price": 299, "image_id": 77}
        state = await scenario([
            model_call("woo__create_product", arguments), model_reply(),
        ], message="Create Tea for 299 with the image", available=TOOLS)
        assert state["status"] == 200
        assert state["executions"] == [
            ("woo__create_product", arguments, {"source": "chat"}),
        ]

        state = await scenario([
            model_call("notes__add_note", {
                "title": "Tea", "body": "Stock arrived.",
            }),
            model_reply(),
        ], message="Add a team note about tea", available=TOOLS)
        assert state["status"] == 200 and len(state["executions"]) == 1
        print("Product/note arguments are preserved; cached replay repeats no actions")

        for failure, status in (
            (provider_limit(), 429),
            (TimeoutError(), 504),
        ):
            state = await scenario([
                model_call("notes__get_note", {"note_id": 1}), failure,
            ], message="Read note 1 and describe its contents", available=TOOLS)
            assert state["status"] == status, state["data"]
            assert len(state["executions"]) == 1
            assert state["data"]["tool_calls"][0]["status"] == "completed"
            assert state["data"]["error"]["automatic_retry_allowed"] is False
        print("Provider rejection and timeout preserve completed activity and replay safely")

        huge = tool("notes__get_note", "Read a note.")
        huge.inputSchema["properties"] = {
            "note_id": {"type": "integer", "enum": list(range(10000))},
        }
        state = await scenario([], message="Read note 1.", available=[huge])
        assert state["status"] == 502, state["data"]
        assert not state["requests"] and not state["executions"]
        print("Definition size protection also covers standalone routes")

        base = await scenario([
            model_call("notes__get_note", {"note_id": 1}), model_reply(),
        ], message="Read note 1 and describe its contents", available=TOOLS)
        costs = [
            ModelBudget.estimate(messages, definitions)
            for messages, definitions, aliases, options in base["requests"]
        ]

        state = await scenario([], message="Read note 1.", available=[
            tool("store-a__get_note", "Read a note."),
            tool("store-b__get_note", "Read a note."),
        ])
        assert state["status"] == 200 and not state["executions"]
        assert "Which store or server" in state["data"]["reply"]
        print("Ambiguous standalone requests require a server choice")

    with patch.dict(os.environ, {
        "CHAT_MODEL_TOKEN_BUDGET": str(sum(costs) - 128),
    }):
        state = await scenario([
            model_call("notes__get_note", {"note_id": 1}),
        ], message="Read note 1 and describe its contents", available=TOOLS)
        assert state["status"] == 429, state["data"]
        assert state["data"]["error"]["reason"] == "chat_model_budget"
        assert state["data"]["error"]["provider"] is None
        assert state["data"]["error"]["phase"] == "after_tools"
        assert len(state["requests"]) == len(state["executions"]) == 1
        assert state["data"]["tool_calls"][0]["status"] == "completed"

    with patch.dict(os.environ, {"CHAT_MODEL_TOKEN_BUDGET": "1000"}):
        state = await scenario([], message="x" * 4000, available=TOOLS)
        assert state["status"] == 429, state["data"]
        assert state["data"]["error"]["phase"] == "before_tools"
        assert not state["requests"] and not state["executions"]

    print("Budget exhaustion stops before the next model request; saved results and replay are preserved")
    print("Chat loop verification passed. No database, model or upstream services were called.")


if __name__ == "__main__":
    asyncio.run(check())
