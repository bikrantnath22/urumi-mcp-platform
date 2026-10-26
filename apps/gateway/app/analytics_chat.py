"""Helpers for unambiguous all-time order-count and sales questions.

Other questions keep the normal chat flow. This module makes no database,
model or network calls. Tool execution still goes through the gateway.
"""
import copy
import re
from decimal import Decimal, InvalidOperation

from app.chat_context import result_data


METRICS = ("order_counts", "sales_summary")


def is_all_time_totals(message):
    value = " ".join(message.casefold().strip().split())
    value = value.replace("\u2011", "-").replace("\u2013", "-")
    value = re.sub(r"[.!?]+$", "", value)
    value = re.sub(r"[.;]\s*do not change anything$", "", value)
    value = re.sub(
        r"^(?:please )?(?:(?:show|get|list|give|tell)(?: me)? "
        r"|what (?:is|are) )?(?:the )?",
        "",
        value,
    )
    order = r"(?:all )?(?:total )?(?:order counts?|orders?)"
    sales = r"(?:all )?(?:total )?sales(?: summary)?"
    time = (
        r"(?: (?:for all time|all[- ]time|overall"
        r"|without date filters|with no date filters))?"
    )
    return re.fullmatch(
        rf"(?:{order}(?: and | & ){sales}"
        rf"|{sales}(?: and | & ){order}){time}",
        value,
    ) is not None


def select_analytics_tools(tools):
    """Return one upstream's permitted tools; None means multiple upstreams."""
    groups = {}
    for tool in tools:
        prefix, separator, suffix = tool.name.partition("__")
        schema = tool.inputSchema or {}
        fields = schema.get("properties") or {}
        if (
            not separator
            or not prefix
            or suffix not in METRICS
            or not tool.annotations
            or tool.annotations.readOnlyHint is not True
            or schema.get("type") != "object"
            or set(fields) != {"date_from", "date_to"}
            or any(fields[key].get("type") != "string" for key in fields)
            or set(schema.get("required") or []) - set(fields)
        ):
            continue
        groups.setdefault(prefix, {})[suffix] = tool

    if len(groups) > 1:
        return None
    if not groups:
        return []

    group = next(iter(groups.values()))
    return [group[metric] for metric in METRICS if metric in group]


def remaining_analytics_tools(tools, trace):
    completed = {
        entry["name"]
        for entry in trace
        if entry.get("status") == "completed"
    }
    return [tool for tool in tools if tool.name not in completed]


def normalize_analytics_calls(completion, permitted_names):
    """Apply the all-time intent consistently before execution and auditing."""
    result = copy.deepcopy(completion)
    calls = result.get("calls") or []
    identities = [call["id"] for call in calls]
    names = [call["name"] for call in calls]
    wire_calls = result["message"].get("tool_calls") or []

    if (
        len(names) != len(set(names))
        or len(identities) != len(set(identities))
        or not set(names).issubset(set(permitted_names))
        or len(wire_calls) != len(calls)
        or {item.get("id") for item in wire_calls} != set(identities)
        or any(
            not isinstance(call.get("arguments"), dict)
            or set(call["arguments"]) - {"date_from", "date_to"}
            for call in calls
        )
    ):
        raise ValueError("Unexpected or repeated analytics call.")

    for call in calls:
        call["arguments"] = {"date_from": "", "date_to": ""}
    for item in wire_calls:
        item["function"]["arguments"] = '{"date_from":"","date_to":""}'
    return result


def _number(value):
    return type(value) is int and 0 <= value <= 10**15


def _all_time_data(entry):
    result = entry.get("result") or {}
    if entry.get("status") != "completed" or result.get("isError"):
        return None

    data = result_data(result)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]

    if (
        not isinstance(data, dict)
        or not {"date_from", "date_to"}.issubset(data)
        or data["date_from"] not in (None, "")
        or data["date_to"] not in (None, "")
    ):
        return None
    return data


def _sales_lines(data):
    groups = data.get("totals_by_currency")
    if (
        data.get("included_statuses") != ["completed", "processing"]
        or not _number(data.get("orders_examined"))
        or not isinstance(groups, list)
        or len(groups) > 50
    ):
        return None

    lines = []
    currencies = set()
    for item in groups:
        if not isinstance(item, dict):
            return None

        currency = item.get("currency")
        amount = item.get("gross_order_total")
        orders = item.get("orders")

        if (
            not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or currency in currencies
            or not _number(orders)
            or not isinstance(amount, str)
            or not re.fullmatch(r"-?\d{1,18}(?:\.\d{1,8})?", amount)
        ):
            return None

        try:
            total = Decimal(amount)
        except InvalidOperation:
            return None

        if not total.is_finite():
            return None

        currencies.add(currency)
        lines.append(
            f"- **{currency} {format(total, 'f')}** ({orders} orders)"
        )

    return lines or [
        "No processing or completed order totals were returned."
    ]


def format_analytics_totals(trace, permitted_names):
    """Format verified results without treating missing totals as zero."""
    values = {}
    for entry in trace:
        if entry.get("name") not in permitted_names:
            continue

        metric = entry["name"].partition("__")[2]
        if metric in values:
            return None, False

        data = _all_time_data(entry)
        if data is not None:
            values[metric] = data

    count = values.get("order_counts") or {}
    count_ok = _number(count.get("total_orders"))
    sales = values.get("sales_summary")
    sales_lines = _sales_lines(sales) if sales is not None else None

    if not count_ok and sales_lines is None:
        return None, False

    lines = ["**All-time analytics results**", ""]
    lines.append(
        f"**Total orders:** {count['total_orders']}"
        if count_ok
        else "**Total orders:** not retrieved."
    )
    lines.extend(["", "**Gross order totals (processing/completed):**"])
    lines.extend(
        sales_lines if sales_lines is not None else ["Not retrieved."]
    )

    if sales_lines is not None:
        lines.extend([
            "",
            "Order values include tax and shipping; partial refunds are not "
            "deducted. Processing orders, including Cash on Delivery, may "
            "not yet be paid.",
        ])

    return "\n".join(lines), count_ok and sales_lines is not None
