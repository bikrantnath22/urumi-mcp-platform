"""Small deterministic context; saved history remains in PostgreSQL.

No extra model call, embeddings, or cross-conversation retrieval is used.
Only a few unambiguous standalone reads omit history and narrow the catalog.
Unknown/compound requests retain compact recent context and authorized tools.
"""
import json
import re

CONTEXT_CHAR_BUDGET = 2400


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def standalone_tool(message):
    value = " ".join(message.strip().lower().split())
    value = re.sub(r"[.!]+$", "", value)
    value = re.sub(r"\.\s*do not change anything$", "", value)
    value = re.sub(r"^please\s+", "", value)
    patterns = (
        (r"(?:list|show|get)(?: me)? (?:the |\d+ |one |two |three |four |five )?products?", "list_products"),
        (r"(?:read|show|get) note #?\s*\d+", "get_note"),
        (r"(?:show|get|list) (?:the )?order counts(?: (?:without|with no) date filters)?", "order_counts"),
    )
    return next((tool for pattern, tool in patterns if re.fullmatch(pattern, value)), None)


def result_data(result):
    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)):
        return structured
    texts = [item.get("text", "") for item in result.get("content", [])
             if item.get("type") == "text"]
    values = []
    for text in texts:
        try:
            values.append(json.loads(text))
        except (ValueError, TypeError):
            values.append(str(text))
    return values[0] if len(values) == 1 else values


def _facts(value):
    if isinstance(value, list):
        # Preserve distinct candidate IDs rather than guessing a target.
        items = [_facts(item) for item in value[:3]]
        return {"items": items, "more_items_omitted": len(value) > 3}
    if not isinstance(value, dict):
        return str(value)[:120]
    keys = ("id", "product_id", "note_id", "image_id", "name", "title",
            "price", "regular_price", "status", "result", "total_orders", "currency")
    facts = {}
    for key in keys:
        if key in value:
            item = value[key]
            if isinstance(item, str):
                facts[key] = item[:90]
            elif item is None or isinstance(item, (int, float, bool)):
                facts[key] = item
    # FastMCP sometimes wraps an array in {"result": [...]}.
    if not facts:
        for key in ("result", "products", "notes", "items"):
            if isinstance(value.get(key), list):
                return {key: _facts(value[key])}
    return facts


def _ids_only(value, depth=0):
    if depth > 3:
        return {"omitted": True}
    if isinstance(value, list):
        return [_ids_only(item, depth + 1) for item in value[:3]]
    if not isinstance(value, dict):
        return {"omitted": True}
    result = {key: item for key, item in value.items()
              if key in {"id", "product_id", "note_id", "image_id"}
              and isinstance(item, int) and not isinstance(item, bool)}
    for key in ("items", "products", "notes", "result"):
        if isinstance(value.get(key), (dict, list)):
            result[key] = _ids_only(value[key], depth + 1)
    result["other_fields_omitted"] = True
    return result


def previous_context(turns, latest_message=""):
    if standalone_tool(latest_message):
        return []
    # Input is newest first. Allocate space to the most recent exchange first.
    selected = []
    used = 0
    for turn in turns:
        response = turn.response or {}
        facts = []
        for call in response.get("tool_calls", [])[:3]:
            result = call.get("result") or {}
            facts.append({"tool": call.get("name"), "status": call.get("status"),
                          "is_error": bool(result.get("isError")),
                          "result": _facts(result_data(result))})
        record = {"user": turn.message[:220], "actual_tool_results": facts}
        if turn.image_id:
            record["attached_image_id"] = turn.image_id
            if response.get("image_tool"):
                record["attached_image_tool"] = response["image_tool"]
        # Keep clarification questions; avoid replaying long prose/tables.
        reply = response.get("reply") or ""
        record["assistant"] = reply[:260]
        if response.get("error"):
            record["unfinished"] = True
        text = packed(record)
        if len(text) > 1200:
            record["assistant"] = reply[:100]
            record["user"] = turn.message[:120]
            for entry in facts:
                entry["result"] = _ids_only(entry["result"])
            text = packed(record)
        if len(text) > 1200:
            # Keep every action's outcome even when the payload must be omitted.
            for entry in facts:
                entry["result"] = {key: item for key, item in entry["result"].items()
                                   if key in {"id", "product_id", "note_id", "image_id"}}
                entry["result"]["other_fields_omitted"] = True
            record["assistant"] = reply[:80]
            text = packed(record)
        if used + len(text) > CONTEXT_CHAR_BUDGET:
            continue
        selected.append({"role": "assistant", "content": text})
        used += len(text)
    return list(reversed(selected))


def model_tool_result(result):
    data = result_data(result)
    encoded = packed(data)
    truncated = len(encoded) > 4000
    return packed({"isError": bool(result.get("isError")),
                   "data": encoded[:4000] if truncated else data,
                   "truncated": truncated})
