"""Pure tool-directory checks; no database, model or upstream calls."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.types import Tool
from app.tool_selection import ToolDirectory, ToolSelectionError


def tool(name, description):
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def rejects(operation):
    try:
        operation()
    except ToolSelectionError:
        return
    raise AssertionError("Invalid selection was accepted")


tools = [
    tool("analytics__order_counts", "Count all store orders by status."),
    tool("analytics__sales_summary", "Summarize gross sales by currency."),
    tool("woo__list_products", "List store products with prices."),
    tool("woo__create_product", "Create a product."),
    tool("notes__add_note", "Save a team note."),
    tool("notes__get_note", "Read a team note."),
]
tools.extend(
    tool(f"service-{number:03d}__health", "Check service health.")
    for number in range(400)
)

directory = ToolDirectory(tools)
result = directory.search(
    "all order count and total sale and list 3 product"
)
assert len(result["items"]) <= 6
selected = directory.load([row["id"] for row in result["items"]])
names = {item.name for item in selected}
assert {
    "analytics__order_counts",
    "analytics__sales_summary",
    "woo__list_products",
}.issubset(names), names
assert "inputSchema" not in json.dumps(result)
print("Mixed request finds the three relevant tools among 406 tools")

for query, expected in (
    ("create product", "woo__create_product"),
    ("add team note", "notes__add_note"),
    ("get note", "notes__get_note"),
):
    first = directory.search(query, limit=1)["items"][0]
    assert directory.load([first["id"]])[0].name == expected
print("Product creation and note tools remain discoverable")

first = directory.search("", limit=8)
second = directory.search("", limit=8, offset=first["next_offset"])
assert len(first["items"]) == len(second["items"]) == 8
assert not {
    item["id"] for item in first["items"]
}.intersection(item["id"] for item in second["items"])
assert directory.search("zzzzunknowncapability")["items"] == []
assert ToolDirectory([]).search("")["items"] == []
print("Bounded search, pagination, unknown queries and empty directories passed")

permitted = ToolDirectory([tools[0]])
assert len(permitted.search("")["items"]) == 1
rejects(lambda: permitted.load(["not-in-this-directory"]))
rejects(lambda: directory.load([first["items"][0]["id"]] * 2))
rejects(lambda: directory.load(["missing"] * 7))
rejects(lambda: directory.load("t00000"))
rejects(lambda: directory.search("orders", limit=True))
rejects(lambda: directory.search("orders", offset=-1))
rejects(lambda: directory.search("x" * 4001))
rejects(lambda: ToolDirectory([tools[0], tools[0]]))
print("Directory scope, duplicate names and invalid selections rejected")

identity = directory.search("create product", limit=1)["items"][0]["id"]
loaded = directory.load([identity])[0]
assert loaded.inputSchema["required"] == ["query"]
assert loaded.inputSchema["properties"]["query"]["maxLength"] == 200
assert loaded.inputSchema["additionalProperties"] is False
loaded.inputSchema["properties"]["query"]["maxLength"] = 999
assert directory.load([identity])[0].inputSchema["properties"]["query"]["maxLength"] == 200
tools[3].inputSchema["required"] = []
assert directory.load([identity])[0].inputSchema["required"] == ["query"]
print("Original schemas and constraints preserved; snapshots and returned tools isolated")
print("Tool directory verification passed. Chat integration is not enabled yet.")
