"""Store-scoped uploads and image chat; all services are fixtures."""

import asyncio
import os
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.update(
    PGPASSWORD="unused-media-fixture",
    JWT_SECRET="media-scope-fixture-" * 3,
    FERNET_KEY=Fernet.generate_key().decode(),
)
sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
               str(Path(__file__).resolve().parent)]

from fastapi import HTTPException
from mcp.types import CallToolResult, TextContent, Tool
from starlette.requests import Request
from app import chat, chat_store, media
from verify_analytics_chat import model_call, model_reply, scenario

NEW = "woo-urumi-tr-02e539a9ea63"
UPLOAD = NEW + "__upload_media"
CREATE = NEW + "__create_product"
USER = SimpleNamespace(id=7)

def tool(name):
    return Tool(name=name, description="Fixture tool", inputSchema={
        "type": "object", "properties": {},
    })

async def nothing(*args, **kwargs):
    pass

async def upload(available, selected=None, result=None):
    calls, received = [], []

    async def receive():
        received.append(True)
        return {"type": "http.request", "body": b"\xff\xd8\xfffixture", "more_body": False}

    async def listed(*args):
        return [tool(name) for name in available]

    async def execute(user, name, arguments, **options):
        calls.append((name, deepcopy(arguments), options))
        return result or CallToolResult(
            content=[], structuredContent={"image_id": 77},
        )

    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": []}, receive)
    with patch.object(media, "release_auth_connection", nothing), \
         patch.object(media, "list_gateway_tools", listed), \
         patch.object(media, "execute_gateway_tool", execute):
        try:
            response = await media.upload_image(request, USER, None, selected)
            return 201, response, calls, received
        except HTTPException as error:
            return error.status_code, error.detail, calls, received

async def image_chat(available, calls):
    body_class = chat.ChatBody

    def image_body(**values):
        return body_class(**values, image_id=77, image_name="Tea.jpg",
                          image_tool=UPLOAD)

    with patch.object(chat, "ChatBody", image_body):
        return await scenario(calls,
            message="Create tea guwahati for 299 with this photo",
            available=[tool(name) for name in available])

async def check():
    status, response, calls, received = await upload([UPLOAD])
    assert status == 201 and response["tool_name"] == UPLOAD
    assert response["image_id"] == 77 and len(calls) == 1
    assert calls[0][0] == UPLOAD and received
    status, response, calls, received = await upload([UPLOAD], "woo__upload_media")
    assert status == 403 and not calls and not received
    status, response, calls, received = await upload([])
    assert status == 403 and not calls and not received
    status, response, calls, received = await upload([UPLOAD, "woo__upload_media"])
    assert status == 409 and not calls and not received
    status, response, calls, received = await upload([UPLOAD, "woo__upload_media"], UPLOAD)
    assert status == 201 and calls[0][0] == UPLOAD
    print("New-store uploads use the exact granted tool; missing access and ambiguous destinations send no write")

    failed = CallToolResult(isError=True, content=[
        TextContent(type="text", text="You do not have permission to use this tool.")])
    status, response, calls, received = await upload([UPLOAD], UPLOAD, failed)
    assert status == 403 and len(calls) == 1
    failed = CallToolResult(isError=True, content=[
        TextContent(type="text", text="private upstream diagnostic")])
    status, response, calls, received = await upload([UPLOAD], UPLOAD, failed)
    assert status == 502 and len(calls) == 1 and "private" not in response
    print("Revocation and uncertain upload errors never fall back to the main store or retry")

    args = {"name": "tea guwahati", "regular_price": 299, "image_id": 77}
    state = await image_chat([UPLOAD, CREATE, "woo__create_product"], [
        model_call(CREATE, args), model_reply("Created"),
    ])
    assert state["status"] == 200 and state["executions"] == [(CREATE, args, {"source": "chat"})]
    assert state["data"]["image_tool"] == UPLOAD
    assert "woo__create_product" not in state["requests"][0][2].values()
    state = await image_chat([UPLOAD, CREATE, "woo__create_product"], [
        model_call("woo__create_product", args),
    ])
    assert state["status"] != 200 and not state["executions"]
    state = await image_chat([UPLOAD, CREATE], [
        model_call(CREATE, {**args, "image_id": 88}),
    ])
    assert state["status"] == 409 and not state["executions"]
    state = await image_chat([CREATE], [])
    assert state["status"] == 403 and not state["requests"] and not state["executions"]
    print("Image chat offers only the selected store; wrong-store actions, changed image IDs and revoked upload grants cannot create products")

    conversation = SimpleNamespace(id=uuid4())
    existing = SimpleNamespace(
        message="Tea", image_id=77, image_name="Tea.jpg",
        response={"image_tool": UPLOAD, "reply": "Saved"},
        status="completed", http_status=200,
    )

    class Database:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, *args): return SimpleNamespace(first=lambda: None)
        async def scalar(self, *args): return conversation
        async def scalars(self, *args): return []
        async def get(self, *args, **kwargs): return existing
        async def commit(self): pass

    with patch.object(chat_store, "Session", Database):
        saved = await chat_store.begin_turn(
            7, conversation.id, uuid4(), "Tea", 77, "Tea.jpg", UPLOAD)
        assert saved["cached"]
        try:
            await chat_store.begin_turn(
                7, conversation.id, uuid4(), "Tea", 77, "Tea.jpg", "woo__upload_media")
        except HTTPException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("A retry changed its image destination")
    print("Saved retries preserve image destination; a changed store conflicts without another model or tool call")
    print("Store image scope verification passed. No live services or data were used.")

if __name__ == "__main__":
    asyncio.run(check())
