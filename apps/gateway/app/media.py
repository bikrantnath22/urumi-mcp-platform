import asyncio
import base64
import json
import re
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.core import execute_gateway_tool, list_gateway_tools
from app.media_scope import ImageScopeError, choose_upload_tool
from app.database import get_session
from app.models import User
from app.tools import release_auth_connection


router = APIRouter(prefix="/api/media", tags=["image upload"])
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def image_extension(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


@router.post("/upload", status_code=201)
async def upload_image(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    tool_name: str | None = Query(default=None, max_length=128),
):
    # Authentication, origin and CSRF checks finish before reading file bytes.
    await release_auth_connection(user, session)
    try:
        selected_tool = choose_upload_tool(await list_gateway_tools(user), tool_name)
    except ImageScopeError as error:
        raise HTTPException(error.status_code, str(error)) from None
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError:
            raise HTTPException(400, "Invalid image size.") from None
        if size < 0:
            raise HTTPException(400, "Invalid image size.")
        if size > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Choose an image of at most 2 MB.")

    data = bytearray()
    try:
        async with asyncio.timeout(20):
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise HTTPException(413, "Choose an image of at most 2 MB.")
                data.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "Image transfer timed out. Please try again.") from None

    if not data:
        raise HTTPException(400, "Choose a nonempty image file.")
    extension = image_extension(data)
    if extension is None:
        raise HTTPException(415, "Choose a JPG, PNG, WebP or GIF image.")

    filename_header = request.headers.get("x-file-name", "product-image")
    if len(filename_header) > 2048:
        raise HTTPException(400, "Image filename is too long.")
    original = unquote(filename_header).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = re.sub(r"[^A-Za-z0-9_-]", "-", original.rsplit(".", 1)[0])[:60].strip("-")
    filename = f"{stem or 'product-image'}.{extension}"

    result = await execute_gateway_tool(
        user,
        selected_tool,
        {"filename": filename, "content_base64": base64.b64encode(data).decode("ascii")},
        source="dashboard",
    )
    if result.isError:
        messages = "\n".join(item.text for item in result.content if item.type == "text")
        if "You do not have permission to use this tool." in messages:
            raise HTTPException(403, "Ask your admin to grant image-upload access.")
        if "Tool server is disabled." in messages:
            raise HTTPException(409, "Image uploads are disabled by your admin.")
        if "Account is unavailable." in messages:
            raise HTTPException(401, "Please sign in again.")
        raise HTTPException(
            502,
            "Image upload could not be confirmed. Ask your admin to check the "
            "WordPress Media Library before uploading the image again.",
        )

    payload = result.structuredContent
    if not isinstance(payload, dict) or "image_id" not in payload:
        payload = None
        for item in result.content:
            if item.type != "text":
                continue
            try:
                candidate = json.loads(item.text)
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, dict) and "image_id" in candidate:
                payload = candidate
                break
    if not payload or type(payload.get("image_id")) is not int or payload["image_id"] <= 0:
        raise HTTPException(
            502, "The image may have uploaded, but its ID was not returned. Ask your admin to check the Media Library."
        )
    return {"image_id": payload["image_id"], "image": payload.get("image"),
            "filename": filename, "tool_name": selected_tool}
