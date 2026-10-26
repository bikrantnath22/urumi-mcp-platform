"""Analytics chat checks with mocked persistence, model and tool boundaries.

Run from apps/gateway: uv run python tests/verify_analytics_chat.py
No database connections, model requests or upstream changes are made.
"""
import asyncio
import copy
import json
import os
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet

# These values exist only inside this verification process.
os.environ.update(
    PGPASSWORD="unused-analytics-fixture",
    JWT_SECRET="analytics-fixture-secret-" * 3,
    FERNET_KEY=Fernet.generate_key().decode(),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from app import analytics_chat, chat
from app.chat_store import public_turn, safe_response
from app.llm import LLMError
from app.rate_limits import RateLimitExceeded


def tool(name, analytics=False):
    fields = {
        key: {"type": "string", "default": ""}
        for key in ("date_from", "date_to")
    } if analytics else {}
    return Tool(
        name=name,
        description="Fixture tool",
        inputSchema={"type": "object", "properties": fields},
        annotations=ToolAnnotations(readOnlyHint=analytics),
    )


CATALOG = [
    tool("analytics__order_counts", True),
    tool("analytics__sales_summary", True),
    tool("woo__create_product"),
    tool("notes__add_note"),
]
COUNTS = {
    "date_from": None,
    "date_to": None,
    "total_orders": 2,
    "by_status": {"processing": 2},
}
SALES = {
    "date_from": None,
    "date_to": None,
    "included_statuses": ["completed", "processing"],
    "orders_examined": 2,
    "totals_by_currency": [
        {"currency": "INR", "orders": 1, "gross_order_total": "299.00"},
        {"currency": "USD", "orders": 1, "gross_order_total": "10.50"},
    ],
}
PROMPT = "all total order count and total sales"
DATES = {"date_from": "", "date_to": ""}


def model_call(name, arguments=None):
    identity = "fixture_" + uuid4().hex
    arguments = {} if arguments is None else arguments
    return {
        "model": "fixture",
        "usage": {},
        "duration_ms": 1,
        "calls": [{
            "id": identity,
            "name": name,
            "arguments": arguments,
        }],
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": identity,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }],
        },
    }


def model_reply(text="Fixture totals."):
    return {
        "model": "fixture",
        "usage": {},
        "duration_ms": 1,
        "calls": [],
        "message": {"role": "assistant", "content": text},
    }


def provider_limit():
    return LLMError(
        "Fixture provider allowance reached.",
        status_code=429,
        retry_after=13,
        provider="groq",
        details={"reason": "tokens_per_minute"},
        request_metrics={
            "outcome": "rejected",
            "usage": {},
            "duration_ms": 1,
        },
    )


async def scenario(
    steps,
    *,
    message=PROMPT,
    available=None,
    failure=None,
    history=None,
):
    state = {
        "requests": [],
        "executions": [],
        "saved": None,
        "cached": False,
    }

    async def begin(*unused):
        if state["cached"]:
            data, status = state["saved"]
            return {
                "cached": True,
                "response": data,
                "http_status": status,
            }
        return {
            "cached": False,
            "conversation_id": uuid4(),
            "turn_id": uuid4(),
            "history": copy.deepcopy(history) if history is not None else [{
                "role": "assistant",
                "content": "Prior price clarification",
            }],
        }

    async def finish(user_id, turn_id, data, status):
        state["saved"] = (copy.deepcopy(data), status)

    async def no_op(*unused):
        pass

    async def listed(*unused):
        return CATALOG if available is None else available

    async def execute(user, name, arguments, **options):
        state["executions"].append(
            (name, copy.deepcopy(arguments), options)
        )
        if name == failure:
            raise RateLimitExceeded("Fixture tools", 5)
        data = (
            COUNTS if name.endswith("__order_counts")
            else SALES if name.endswith("__sales_summary")
            else {"id": 901}
        )
        return CallToolResult(
            isError=False,
            content=[TextContent(type="text", text=json.dumps(data))],
        )

    async def complete(client, messages, definitions, aliases, **options):
        state["requests"].append((
            copy.deepcopy(messages),
            copy.deepcopy(definitions),
            dict(aliases),
            dict(options),
        ))
        if not steps:
            raise AssertionError("Unexpected extra model request")
        value = steps.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *unused):
            return False

    with ExitStack() as stack:
        for name, replacement in (
            ("begin_turn", begin),
            ("finish_turn", finish),
            ("save_progress", no_op),
            ("release_auth_connection", no_op),
            ("list_gateway_tools", listed),
            ("execute_gateway_tool", execute),
            ("create_llm_client", Client),
            ("request_completion", complete),
        ):
            stack.enter_context(patch.object(chat, name, replacement))

        body = chat.ChatBody(message=message)
        user = SimpleNamespace(id=7)
        response = await chat.chat(body, user, None)
        state["status"] = response.status_code
        state["data"] = json.loads(response.body)

        counts_before = (
            len(state["requests"]),
            len(state["executions"]),
        )
        state["cached"] = True
        replay = await chat.chat(body, user, None)

        assert json.loads(replay.body) == state["data"]
        assert replay.status_code == response.status_code
        assert counts_before == (
            len(state["requests"]),
            len(state["executions"]),
        ), "Replay repeated a model or tool call"

    return state


async def check():
    state = await scenario([
        model_call("analytics__sales_summary", {
            "date_from": "2026-10-01",
            "date_to": "2026-10-16",
        }),
        model_call("analytics__order_counts"),
        model_reply(),
    ])
    assert state["status"] == 200
    assert [item[0] for item in state["executions"]] == [
        "analytics__sales_summary",
        "analytics__order_counts",
    ]
    assert all(
        args == DATES and options == {"source": "chat"}
        for _, args, options in state["executions"]
    )

    requests = state["requests"]
    assert set(requests[0][2]) == {
        "analytics__order_counts",
        "analytics__sales_summary",
    }
    assert set(requests[1][2]) == {"analytics__order_counts"}
    assert requests[2][1:3] == ([], {})
    assert "Prior price clarification" not in json.dumps(requests)

    wire = requests[1][0][-2]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(wire) == DATES
    print(
        "Both totals read once; all-time dates, narrowed catalog "
        "and schema-free final answer passed"
    )

    state = await scenario([
        model_call("analytics__order_counts"),
        model_call("analytics__sales_summary"),
        provider_limit(),
    ])
    assert state["status"] == 429
    data = state["data"]
    assert data["error"]["results_complete"] is True
    assert data["error"]["automatic_retry_allowed"] is False
    assert "Total orders:** 2" in data["reply"]
    assert "INR 299.00" in data["reply"]
    assert "USD 10.50" in data["reply"]
    assert "not yet be paid" in data["reply"]
    assert len(data["model_requests"]) == 3
    assert data == state["saved"][0]
    saved_turn = SimpleNamespace(
        id=uuid4(), message=PROMPT, image_id=None, image_name="",
        created_at=datetime.now(timezone.utc),
        response=safe_response(data), status="error",
    )
    view = public_turn(saved_turn)
    assert view["results_complete"] is True
    assert view["reply"] == data["reply"]
    saved_turn.response["error"]["results_complete"] = False
    assert public_turn(saved_turn)["results_complete"] is False
    saved_turn.response["error"] = {"message": "Older saved error"}
    assert public_turn(saved_turn)["results_complete"] is False
    print("Saved completion status survives history serialization; older messages remain supported")
    print(
        "Provider failure preserves readable totals, separate currencies, "
        "diagnostics and replay without repeated calls"
    )

    state = await scenario([
        model_call("analytics__order_counts"),
        model_call("analytics__sales_summary"),
    ], failure="analytics__sales_summary")
    assert state["status"] == 429
    assert state["data"]["error"]["results_complete"] is False
    assert "Total orders:** 2" in state["data"]["reply"]
    assert "Not retrieved." in state["data"]["reply"]
    assert state["data"]["tool_calls"][1]["status"] == "rate_limited"

    state = await scenario([provider_limit()])
    assert not state["executions"]
    assert state["data"]["reply"] is None

    state = await scenario([
        model_call("analytics__order_counts"),
        model_call("analytics__order_counts"),
    ])
    assert state["status"] == 502
    assert len(state["executions"]) == 1
    print(
        "Partial results, provider rejection before execution "
        "and repeated-read prevention passed"
    )

    state = await scenario([
        model_call("analytics__order_counts"),
        model_reply("Order count retrieved; sales is not granted."),
    ], available=CATALOG[:1])
    assert state["status"] == 200
    assert set(state["requests"][0][2]) == {"analytics__order_counts"}
    assert len(state["executions"]) == 1

    state = await scenario([], available=[])
    assert not state["requests"]
    assert not state["executions"]

    state = await scenario([], available=[
        *CATALOG[:2],
        tool("second-store__order_counts", True),
    ])
    assert not state["requests"]
    assert not state["executions"]
    assert "Which store" in state["data"]["reply"]
    print(
        "Missing grants never invent tools; "
        "multiple analytics servers require clarification"
    )

    for message, name, arguments in (
        (
            "Create Assam Tea for 299",
            "woo__create_product",
            {"name": "Assam Tea", "regular_price": 299},
        ),
        (
            "Add a note about tea",
            "notes__add_note",
            {"title": "Tea", "body": "Fixture note"},
        ),
        (
            "Create a product and show total orders and sales",
            "woo__create_product",
            {"name": "Tea", "regular_price": 99},
        ),
    ):
        state = await scenario([
            model_call(name, arguments),
            model_reply(),
        ], message=message)
        assert state["status"] == 200
        assert state["executions"] == [
            (name, arguments, {"source": "chat"}),
        ]
        assert set(state["requests"][0][2]) == {
            item.name for item in CATALOG
        }
        assert "Prior price clarification" in json.dumps(
            state["requests"][0][0]
        )
    print(
        "Product, note and mixed requests retain "
        "the general flow and original arguments"
    )

    malformed = copy.deepcopy(SALES)
    malformed["totals_by_currency"][0]["gross_order_total"] = "NaN"
    trace = [{
        "name": "analytics__sales_summary",
        "status": "completed",
        "result": {
            "isError": False,
            "structuredContent": malformed,
        },
    }]
    assert analytics_chat.format_analytics_totals(
        trace,
        {"analytics__sales_summary"},
    ) == (None, False)

    print("Malformed totals cannot become a successful fallback")
    print(
        "Analytics chat verification passed. "
        "No database, model or upstream calls made."
    )


if __name__ == "__main__":
    asyncio.run(check())
