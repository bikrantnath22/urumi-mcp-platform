import asyncio
import os
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse


BASE_URL = os.getenv("WOO_BASE_URL", "http://wordpress").rstrip("/")
HOST_HEADER = os.getenv("WOO_HOST_HEADER", "store.localtest.me")
KEY = os.getenv("WC_KEY", "")
SECRET = os.getenv("WC_SECRET", "")

if not KEY or not SECRET:
    raise SystemExit("Set WC_KEY and WC_SECRET to the read-only credentials.")

mcp = FastMCP(
    "analytics",
    host=os.getenv("BIND_HOST", "127.0.0.1"),
    port=int(os.getenv("PORT", "8001")),
    stateless_http=True,
    json_response=True,
)

READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SALES_STATUSES = {"processing", "completed"}
MAX_PAGES = 10
PAGE_SIZE = 100


def date_filters(date_from: str, date_to: str) -> dict:
    """Validate inclusive calendar dates in the WordPress site timezone."""
    params = {}
    start = None
    end = None

    try:
        if date_from:
            start = date.fromisoformat(date_from)
            if start.isoformat() != date_from:
                raise ValueError
            params["after"] = f"{date_from}T00:00:00"

        if date_to:
            end = date.fromisoformat(date_to)
            if end.isoformat() != date_to:
                raise ValueError
            params["before"] = f"{date_to}T23:59:59"
    except ValueError:
        raise ValueError("Dates must use YYYY-MM-DD, for example 2026-10-12.")

    if start and end and start > end:
        raise ValueError("date_from must not be later than date_to.")

    return params


async def fetch_orders(date_from: str, date_to: str) -> list[dict]:
    params = {
        "status": "any",
        "per_page": PAGE_SIZE,
        "orderby": "id",
        "order": "asc",
        "_fields": "id,status,currency,total,line_items",
        **date_filters(date_from, date_to),
    }

    headers = {"X-Forwarded-Proto": "https"}
    if HOST_HEADER:
        headers["Host"] = HOST_HEADER

    orders = []

    try:
        async with asyncio.timeout(25):
            async with httpx.AsyncClient(
                auth=(KEY, SECRET),
                headers=headers,
                timeout=10,
                follow_redirects=False,
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    response = await client.get(
                        f"{BASE_URL}/wp-json/wc/v3/orders",
                        params={**params, "page": page},
                    )

                    if response.status_code in (401, 403):
                        raise RuntimeError(
                            "WooCommerce rejected the read-only credentials."
                        )

                    if response.is_redirect:
                        raise RuntimeError(
                            "WooCommerce redirected the request. "
                            "Check WOO_BASE_URL and WOO_HOST_HEADER."
                        )

                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"WooCommerce returned HTTP {response.status_code}."
                        )

                    batch = response.json()
                    if not isinstance(batch, list):
                        raise RuntimeError("Unexpected WooCommerce response.")

                    orders.extend(
                        order for order in batch
                        if order["status"] != "trash"
                    )

                    total_pages = response.headers.get("X-WP-TotalPages")
                    if total_pages:
                        pages = int(total_pages)
                        if pages > MAX_PAGES:
                            raise RuntimeError(
                                "More than 1,000 orders match. "
                                "Use a narrower date range."
                            )
                        if page >= pages:
                            return orders
                    elif len(batch) < PAGE_SIZE:
                        return orders

    except (TimeoutError, httpx.TimeoutException):
        raise RuntimeError(
            "Analytics request timed out. Try a narrower date range."
        )
    except httpx.RequestError:
        raise RuntimeError(
            "Cannot reach WooCommerce. Check the WordPress connection."
        )

    raise RuntimeError(
        "Pagination limit reached. Use a narrower date range."
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@mcp.tool(annotations=READ)
async def sales_summary(date_from: str = "", date_to: str = "") -> dict:
    """Summarize processing/completed orders by creation date.
    Optional inclusive YYYY-MM-DD dates use the WordPress site timezone.
    Totals include tax/shipping and do not deduct partial refunds.
    They are order values, not proof of payment received.
    """
    orders = await fetch_orders(date_from, date_to)
    groups = {}

    for order in orders:
        if order["status"] not in SALES_STATUSES:
            continue

        currency = order["currency"]
        if currency not in groups:
            groups[currency] = {
                "orders": 0,
                "total": Decimal("0"),
            }

        groups[currency]["orders"] += 1
        groups[currency]["total"] += Decimal(order["total"])

    return {
        "date_from": date_from or None,
        "date_to": date_to or None,
        "included_statuses": sorted(SALES_STATUSES),
        "orders_examined": len(orders),
        "totals_by_currency": [
            {
                "currency": currency,
                "orders": group["orders"],
                "gross_order_total": format(group["total"], "f"),
            }
            for currency, group in sorted(groups.items())
        ],
        "basis": (
            "Order creation dates; totals include tax/shipping. "
            "Partial refunds are not deducted. "
            "Processing COD orders may not yet be paid."
        ),
    }


@mcp.tool(annotations=READ)
async def order_counts(date_from: str = "", date_to: str = "") -> dict:
    """Count non-trashed orders by status and creation date.
    Optional inclusive dates use YYYY-MM-DD in the WordPress site timezone.
    Empty dates mean all available history.
    """
    orders = await fetch_orders(date_from, date_to)
    counts = Counter(order["status"] for order in orders)

    return {
        "date_from": date_from or None,
        "date_to": date_to or None,
        "total_orders": len(orders),
        "by_status": dict(sorted(counts.items())),
    }


@mcp.tool(annotations=READ)
async def top_products(
    limit: int = 5,
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Rank products/variations by units in processing/completed orders.
    Optional inclusive dates filter order creation dates in the site timezone.
    Refunded orders are excluded; partial refunds are not deducted.
    """
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20.")

    orders = await fetch_orders(date_from, date_to)
    quantities = defaultdict(int)
    names = {}

    for order in orders:
        if order["status"] not in SALES_STATUSES:
            continue

        for item in order.get("line_items", []):
            key = (
                item["product_id"],
                item.get("variation_id", 0),
            )
            quantities[key] += int(item["quantity"])
            names[key] = item["name"]

    ranked = sorted(
        quantities,
        key=lambda key: (-quantities[key], key),
    )[:limit]

    return {
        "date_from": date_from or None,
        "date_to": date_to or None,
        "included_statuses": sorted(SALES_STATUSES),
        "products": [
            {
                "product_id": key[0],
                "variation_id": key[1],
                "name": names[key],
                "units_ordered": quantities[key],
            }
            for key in ranked
        ],
        "basis": "Ordered quantities; partial refunds are not deducted.",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")