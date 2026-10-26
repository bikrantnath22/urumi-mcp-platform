import base64
import math
import os
import re
from typing import Literal
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

BASE_URL = os.environ.get("WOO_BASE_URL", "http://wordpress").rstrip("/")
HOST_HEADER = os.environ.get("WOO_HOST_HEADER", "")
KEY = os.environ.get("WC_KEY", "")
SECRET = os.environ.get("WC_SECRET", "")
API = f"{BASE_URL}/wp-json/wc/v3"
MEDIA_API = f"{BASE_URL}/wp-json/wp/v2/media"
MEDIA_USER = os.environ.get("WP_MEDIA_USER", "")
MEDIA_PASSWORD = os.environ.get("WP_MEDIA_APP_PASSWORD", "")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # WordPress default upload limit
PUBLIC_STORE_URL = os.environ.get("PUBLIC_STORE_URL", "").rstrip("/")
PUBLIC_ORIGIN = urlparse(PUBLIC_STORE_URL) if PUBLIC_STORE_URL else None
if PUBLIC_ORIGIN is not None:
    try:
        PUBLIC_ORIGIN.port
        valid_origin = (
            PUBLIC_ORIGIN.scheme in {"http", "https"}
            and bool(PUBLIC_ORIGIN.hostname)
            and PUBLIC_ORIGIN.username is None
            and PUBLIC_ORIGIN.password is None
            and not PUBLIC_ORIGIN.path
            and not PUBLIC_ORIGIN.query
            and not PUBLIC_ORIGIN.fragment
        )
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise SystemExit("PUBLIC_STORE_URL must be an HTTP(S) origin without credentials or a path.")

if not KEY or not SECRET:
    raise SystemExit("WC_KEY and WC_SECRET must be set")

mcp = FastMCP(
    "woocommerce",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
    json_response=True,
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]
OrderFilter = Literal["any", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]


def _headers() -> dict:
    h = {"X-Forwarded-Proto": "https"}
    if HOST_HEADER:
        h["Host"] = HOST_HEADER
    return h


async def _call(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(timeout=30, auth=(KEY, SECRET), headers=_headers()) as client:
        try:
            r = await client.request(method, f"{API}{path}", **kwargs)
        except httpx.TimeoutException:
            raise RuntimeError("WooCommerce did not answer within 30 seconds")
        except httpx.HTTPError as e:
            raise RuntimeError(f"Cannot reach WooCommerce ({type(e).__name__})")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text[:200])
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"WooCommerce error {r.status_code}: {msg}")
    return r.json()


def _public_url(value):
    """Match store links to its configured public origin; leave other hosts alone."""
    if PUBLIC_ORIGIN is None or not isinstance(value, str):
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != PUBLIC_ORIGIN.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return value
    return parsed._replace(
        scheme=PUBLIC_ORIGIN.scheme,
        netloc=PUBLIC_ORIGIN.netloc,
    ).geturl()


def _product(p: dict) -> dict:
    return {
        "id": p["id"],
        "name": p["name"],
        "status": p["status"],
        "price": p.get("price"),
        "regular_price": p.get("regular_price"),
        "stock_status": p.get("stock_status"),
        "url": _public_url(p.get("permalink")),
        "image": _public_url((p.get("images") or [{}])[0].get("src")),
        "image_id": (p.get("images") or [{}])[0].get("id"),
    }


def _order(o: dict) -> dict:
    b = o.get("billing", {})
    return {
        "id": o["id"],
        "status": o["status"],
        "total": o["total"],
        "currency": o["currency"],
        "date_created": o["date_created"],
        "customer": f'{b.get("first_name", "")} {b.get("last_name", "")}'.strip(),
        "payment_method": o.get("payment_method_title"),
        "items": [
            {"product_id": i["product_id"], "name": i["name"], "quantity": i["quantity"], "total": i["total"]}
            for i in o.get("line_items", [])
        ],
    }


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@mcp.tool(annotations=READ)
async def list_products(
    search: str = "",
    status: Literal["any", "publish", "draft", "pending", "private", "trash"] = "any",
    per_page: int = 20,
    page: int = 1,
) -> list[dict]:
    """List store products. Optional text search and status filter."""
    params = {"per_page": max(1, min(per_page, 100)), "page": max(1, page), "status": status}
    if search:
        params["search"] = search
    return [_product(p) for p in await _call("GET", "/products", params=params)]


def _price(value: float, field: str) -> str:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be a finite, non-negative number")
    return f"{value:.2f}"


@mcp.tool(annotations=WRITE)
async def create_product(
    name: str,
    regular_price: float,
    description: str = "",
    image_id: int = 0,
    image_url: str = "",
    status: Literal["publish", "draft"] = "publish",
) -> dict:
    """Create a simple product with a name, a price and optionally a photo.
    For the photo give EITHER image_id (the ID of an image already uploaded to the store's
    Media Library, also shown as image_id in list_products) OR image_url (a public http(s)
    link to a JPG, PNG or WebP image that the store downloads). Not both.
    Published products appear on the storefront."""
    price = _price(regular_price, "regular_price")
    if image_id and image_url:
        raise ValueError("give either image_id or image_url, not both")
    body = {
        "name": name,
        "type": "simple",
        "regular_price": price,
        "description": description,
        "status": status,
    }
    if image_id:
        if image_id < 0:
            raise ValueError("image_id must be a positive number")
        body["images"] = [{"id": image_id}]
    elif image_url:
        parsed = urlparse(image_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("image_url must be a full http(s) link")
        body["images"] = [{"src": image_url}]
    return _product(await _call("POST", "/products", json=body))


ProductStatus = Literal["publish", "draft", "pending", "private"]


@mcp.tool(annotations=READ)
async def get_product(product_id: int) -> dict:
    """Get one product by its numeric id."""
    return _product(await _call("GET", f"/products/{product_id}"))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def update_product(
    product_id: int,
    name: str | None = None,
    regular_price: float | None = None,
    sale_price: float | None = None,
    description: str | None = None,
    status: ProductStatus | None = None,
    stock_status: Literal["instock", "outofstock"] | None = None,
    image_id: int | None = None,
    image_url: str | None = None,
) -> dict:
    """Change an existing product. Only the fields you give are changed.
    sale_price 0 removes the sale price. status draft hides the product from the storefront.
    For a new photo give EITHER image_id (Media Library) OR image_url, not both."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    if regular_price is not None:
        body["regular_price"] = _price(regular_price, "regular_price")
    if sale_price is not None:
        price = _price(sale_price, "sale_price")
        body["sale_price"] = "" if sale_price == 0 else price
    if description is not None:
        body["description"] = description
    if status is not None:
        body["status"] = status
    if stock_status is not None:
        body["stock_status"] = stock_status
    if image_id and image_url:
        raise ValueError("give either image_id or image_url, not both")
    if image_id:
        if image_id < 0:
            raise ValueError("image_id must be a positive number")
        body["images"] = [{"id": image_id}]
    elif image_url:
        parsed = urlparse(image_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("image_url must be a full http(s) link")
        body["images"] = [{"src": image_url}]
    if not body:
        raise ValueError("nothing to update: give at least one field to change")
    return _product(await _call("PUT", f"/products/{product_id}", json=body))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_product(product_id: int, permanent: bool = False) -> dict:
    """Delete a product. By default it is moved to the Trash and can be restored in wp-admin
    (Products, then Trash). With permanent=true it is erased for good and cannot be restored.
    Always confirm with the user before calling this."""
    r = await _call("DELETE", f"/products/{product_id}", params={"force": "true" if permanent else "false"})
    return {
        "id": r["id"],
        "name": r["name"],
        "result": "permanently deleted" if permanent else "moved to trash",
    }


@mcp.tool(annotations=READ)
async def list_orders(
    status: OrderFilter = "any",
    after: str = "",
    before: str = "",
    per_page: int = 20,
    page: int = 1,
) -> list[dict]:
    """List orders, newest first. after/before are ISO-8601 timestamps such as 2026-10-11T00:00:00."""
    params = {
        "per_page": max(1, min(per_page, 100)),
        "page": max(1, page),
        "status": status,
        "orderby": "date",
        "order": "desc",
    }
    if after:
        params["after"] = after
    if before:
        params["before"] = before
    return [_order(o) for o in await _call("GET", "/orders", params=params)]


@mcp.tool(annotations=READ)
async def get_order(order_id: int) -> dict:
    """Get one order by its numeric id."""
    return _order(await _call("GET", f"/orders/{order_id}"))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def update_order_status(order_id: int, status: OrderStatus) -> dict:
    """Change the status of an order, for example from processing to completed."""
    return _order(await _call("PUT", f"/orders/{order_id}", json={"status": status}))


def _sniff_image(data: bytes):
    """Decide the real image type from the first bytes, never from the file name."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    return None


@mcp.tool(annotations=WRITE)
async def upload_media(filename: str, content_base64: str) -> dict:
    """Upload an image (JPEG, PNG, WebP or GIF, at most 2 MB, given as base64) to the store's
    Media Library. Returns image_id, which can be passed to create_product."""
    if not MEDIA_USER or not MEDIA_PASSWORD:
        raise ValueError("media uploads are not configured on this server")
    if len(content_base64) > MAX_UPLOAD_BYTES * 4 // 3 + 16:
        raise ValueError("image is larger than 2 MB")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception:
        raise ValueError("content_base64 is not valid base64")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image is empty or larger than 2 MB")
    kind = _sniff_image(data)
    if not kind:
        raise ValueError("only JPEG, PNG, WebP and GIF images are accepted")
    mime, ext = kind
    stem = re.sub(r"[^A-Za-z0-9_-]", "-", filename.rsplit(".", 1)[0])[:60].strip("-") or "upload"
    headers = _headers()
    headers["Content-Type"] = mime
    headers["Content-Disposition"] = f'attachment; filename="{stem}.{ext}"'
    async with httpx.AsyncClient(timeout=60, auth=(MEDIA_USER, MEDIA_PASSWORD), headers=headers) as client:
        try:
            r = await client.post(MEDIA_API, content=data)
        except httpx.TimeoutException:
            raise RuntimeError("WordPress did not answer within 60 seconds")
        except httpx.HTTPError as e:
            raise RuntimeError(f"Cannot reach WordPress ({type(e).__name__})")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text[:200])
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"WordPress error {r.status_code}: {msg}")
    j = r.json()
    return {"image_id": j["id"], "image": _public_url(j.get("source_url"))}


async def _wp_call(method: str, path: str, **kwargs):
    """Call the WordPress REST API as the media manager user."""
    if not MEDIA_USER or not MEDIA_PASSWORD:
        raise ValueError("media management is not configured on this server")
    async with httpx.AsyncClient(timeout=30, auth=(MEDIA_USER, MEDIA_PASSWORD), headers=_headers()) as client:
        try:
            r = await client.request(method, f"{BASE_URL}/wp-json/wp/v2{path}", **kwargs)
        except httpx.TimeoutException:
            raise RuntimeError("WordPress did not answer within 30 seconds")
        except httpx.HTTPError as e:
            raise RuntimeError(f"Cannot reach WordPress ({type(e).__name__})")
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text[:200])
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"WordPress error {r.status_code}: {msg}")
    return r.json()


def _media(m: dict) -> dict:
    return {
        "id": m["id"],
        "title": (m.get("title") or {}).get("rendered"),
        "alt_text": m.get("alt_text"),
        "caption": re.sub(r"<[^>]+>", "", (m.get("caption") or {}).get("rendered", "")).strip(),
        "type": m.get("mime_type"),
        "url": _public_url(m.get("source_url")),
        "uploaded": m.get("date"),
    }


@mcp.tool(annotations=READ)
async def list_media(search: str = "", per_page: int = 20, page: int = 1) -> list[dict]:
    """List images in the store's Media Library, newest first. Use search to find an image by
    its title or file name, then pass its id as image_id to create_product or update_product."""
    params = {
        "media_type": "image",
        "per_page": max(1, min(per_page, 100)),
        "page": max(1, page),
        "orderby": "date",
        "order": "desc",
    }
    if search:
        params["search"] = search
    return [_media(m) for m in await _wp_call("GET", "/media", params=params)]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def update_media(
    media_id: int,
    title: str | None = None,
    alt_text: str | None = None,
    caption: str | None = None,
    description: str | None = None,
) -> dict:
    """Change the details of a Media Library image: title, alt text (for accessibility),
    caption or description. Only the fields you give are changed. The picture itself cannot
    be replaced: upload a new image and use its id instead."""
    body = {
        k: v
        for k, v in {"title": title, "alt_text": alt_text, "caption": caption, "description": description}.items()
        if v is not None
    }
    if not body:
        raise ValueError("nothing to update: give at least one field to change")
    return _media(await _wp_call("POST", f"/media/{media_id}", json=body))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_media(media_id: int, confirm: bool = False) -> dict:
    """Permanently delete an image from the Media Library. WordPress has no trash for media, so
    this cannot be undone, and products that use the image will show a placeholder.
    First tell the user which image will be deleted and get their yes, then call with confirm=true."""
    if not confirm:
        raise ValueError(
            "Deleting media is permanent. Tell the user which image will be deleted, "
            "get their confirmation, then call again with confirm=true."
        )
    r = await _wp_call("DELETE", f"/media/{media_id}", params={"force": "true"})
    prev = r.get("previous") or {}
    return {
        "id": media_id,
        "title": (prev.get("title") or {}).get("rendered"),
        "result": "permanently deleted" if r.get("deleted") else "not deleted",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")