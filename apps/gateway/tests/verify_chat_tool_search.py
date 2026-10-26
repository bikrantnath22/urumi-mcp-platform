"""Pure search checks; no database, model or upstream requests."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.types import Tool
from app.chat_tool_search import (
    ChatToolSearch, MAX_SCHEMA_CHARACTERS, SEARCH_TOOL_NAME,
)
from app.tool_selection import ToolSelectionError


def tool(name, description, properties=None):
    return Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": properties or {}},
    )


def search(query="", offset=0):
    return [{
        "name": SEARCH_TOOL_NAME,
        "arguments": {"query": query, "offset": offset},
    }]


def rejects(operation):
    try:
        operation()
    except ToolSelectionError:
        return
    raise AssertionError("Invalid request or repeated search was accepted")


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

selected = ChatToolSearch(
    tools, "all order count and total sale and list 3 product", []
)
definitions, aliases = selected.catalog()
assert {
    "analytics__order_counts",
    "analytics__sales_summary",
    "woo__list_products",
}.issubset(aliases)
assert SEARCH_TOOL_NAME in aliases
assert len(aliases) <= 5
assert len(json.dumps(
    definitions, ensure_ascii=False, separators=(",", ":")
)) <= MAX_SCHEMA_CHARACTERS
print("Mixed request loads relevant tools from a catalogue of 406")

for offset in range(0, 48, 6):
    page = json.loads(selected.search(search(offset=offset)))
    assert sum(item["loaded"] for item in page["items"]) <= 4
    definitions, aliases = selected.catalog()
    assert len(aliases) <= 5
    assert len(json.dumps(
        definitions, ensure_ascii=False, separators=(",", ":")
    )) <= MAX_SCHEMA_CHARACTERS
assert selected.searches == 8
assert SEARCH_TOOL_NAME in selected.catalog()[1]
print("Eight distinct searches passed; there is no fixed search-count cap")

before = selected.searches
rejects(lambda: selected.search(search(offset=42)))
assert selected.searches == before
selected.search(search(offset=42), progress=1)
assert selected.searches == before + 1
rejects(lambda: selected.search(search(offset=42), progress=1))
print("Repeated results stop without progress; a completed action allows a fresh lookup")

empty = ChatToolSearch(tools, "unmatched capability", [])
empty.search(search("zzzzunknown"))
rejects(lambda: empty.search(search("yyyyunknown")))
rejects(lambda: empty.search(search("note"), progress=True))
rejects(lambda: empty.search([{
    "name": SEARCH_TOOL_NAME,
    "arguments": {"query": "note", "extra": True},
}]))
rejects(lambda: empty.search([
    *search("note"),
    {"name": "woo__create_product", "arguments": {}},
]))
empty.validate_calls([])
rejects(lambda: empty.validate_calls([{"name": "private__delete_record"}]))
print("Empty-result loops, invalid searches and mixed search/action calls are rejected")

scoped = ChatToolSearch(tools[:1], "orders", [])
assert set(scoped.catalog()[1]) == {"analytics__order_counts"}
rejects(lambda: scoped.validate_calls([{"name": "notes__get_note"}]))
rejects(lambda: scoped.search(search("note")))
first = ChatToolSearch(tools, "unmatched capability", [])
second = ChatToolSearch(tools, "unmatched capability", [])
first.search(search("get note"))
assert "notes__get_note" in first.catalog()[1]
assert "notes__get_note" not in second.catalog()[1]
assert second.searches == 0
print("Tool scope and search state remain separate for each turn")

original = tool("notes__add_note", "Save a note.", {
    "title": {"type": "string", "maxLength": 200},
})
snapshot = ChatToolSearch([original], "add note", [])
original.inputSchema["properties"]["title"]["maxLength"] = 999
definitions, _ = snapshot.catalog()
assert definitions[0]["function"]["parameters"]["properties"]["title"]["maxLength"] == 200
definitions[0]["function"]["parameters"]["properties"]["title"]["maxLength"] = 777
assert snapshot.catalog()[0][0]["function"]["parameters"]["properties"]["title"]["maxLength"] == 200

huge = tool("large__get_record", "Read a large record.", {
    "value": {"type": "string", "enum": ["x" * 10000]},
})
large = ChatToolSearch([huge], "record", [])
page = json.loads(large.search(search("record")))
assert page["items"] and not page["items"][0]["loaded"]
assert huge.inputSchema["properties"]["value"]["enum"] == ["x" * 10000]
rejects(lambda: large.validate_calls([{"name": huge.name}]))
print("Schemas stay intact and isolated; oversized definitions are withheld explicitly")
# A compound request needs both capabilities even when several stores expose
# similar order tools and the request starts with the word "Get".
order_tool = "analytics-test2-222222222222__order_counts"
compound = "Get today's order count for test2, then save the result in Team Notes titled test2 daily orders."
managed = [
    tool("notes__add_note", "[Team Notes] Save a shared team note.", {
        "title": {"type": "string"}, "body": {"type": "string"},
    }),
    tool("notes__get_note", "[Team Notes] Read a complete shared team note by ID."),
    tool("notes__list_notes", "[Team Notes] List shared team notes."),
    tool("notes__info", "[Team Notes] Describe the notes server."),
]
for slug, identity in (("test1", "111111111111"), ("test2", "222222222222"),
                       ("cloth-assam", "333333333333"), ("urumi", "444444444444")):
    managed.extend([
        tool(f"analytics-{slug}-{identity}__order_counts",
             f"[{slug} / Analytics] Count non-trashed orders by status and creation date.", {
                 "date_from": {"type": "string"}, "date_to": {"type": "string"},
             }),
        tool(f"woo-{slug}-{identity}__get_order",
             f"[{slug} / WooCommerce] Get one order by its numeric id."),
    ])
selected = ChatToolSearch(managed, compound, [])
assert {order_tool, "notes__add_note"}.issubset(selected.catalog()[1]), "Compound request lost a required upstream"
for query in ("notes__add_note", "test2 order_counts"):
    page = json.loads(selected.search(search(query)))
    definitions, aliases = selected.catalog()
    assert {order_tool, "notes__add_note"}.issubset(aliases), "Search discarded the other requested capability"
    encoded = json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
    assert len(aliases) <= 5 and len(encoded) <= MAX_SCHEMA_CHARACTERS
    assert all(item["loaded"] == (item["name"] in aliases) for item in page["items"])
    selected.validate_calls([{"name": order_tool}, {"name": "notes__add_note"}])
print("Compound order-count/note requests keep both upstreams loaded across searches within the existing limits")
long_name = "s" * 110 + "__read_record"
long = ChatToolSearch([
    tool(long_name, "Read a record."),
    *[tool(f"other-{index}__health", "Check health.") for index in range(4)],
], "record", [])
page = json.loads(long.search(search("record")))
assert page["items"][0]["loaded"] is True
assert len(page["items"][0]["name"]) == 120
long.validate_calls([{"name": long_name}])
print("Long qualified names keep correct loaded flags after display-name shortening")
unrelated = ChatToolSearch(tools, "Look up the parcel progress", [])
unrelated.search(search("", offset=12))
unrelated.search(search("order count and total sale and list 3 product"))
assert {"analytics__order_counts", "analytics__sales_summary", "woo__list_products"}.issubset(unrelated.catalog()[1])
assert len(unrelated.catalog()[1]) <= 5
print("A combined lookup can still load three new actions after unrelated searches")
print("Search helper verification passed. No live services were called.")
