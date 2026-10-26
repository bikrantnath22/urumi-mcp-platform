"""Public Traefik ForwardAuth gate. Never returns account or store data."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Store

router = APIRouter()
_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}
_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Store temporarily unavailable</title>
<style>body{font:16px system-ui;background:#fafafa;color:#171717;display:grid;
place-items:center;min-height:90vh;margin:0}main{max-width:30rem;padding:2rem}
h1{font-size:1.6rem}p{line-height:1.7;color:#525252}</style>
<main><h1>This store is temporarily unavailable</h1>
<p>Please check back later. Your store owner can contact the platform administrator.</p></main></html>"""


@router.get("/api/store-access/{store_id}", include_in_schema=False)
async def store_access_gate(
    store_id: UUID, session: AsyncSession = Depends(get_session),
):
    try:
        async with asyncio.timeout(3):
            allowed = await session.scalar(select(Store.id).where(
                Store.id == store_id, Store.status == "Ready",
                Store.desired_state == "present", Store.deleted_at.is_(None),
                Store.access_enabled.is_(True), Store.access_guarded.is_(True),
            ))
        if allowed is not None:
            return Response(status_code=204, headers=_HEADERS)
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
    return HTMLResponse(
        _PAGE, status_code=503, headers={**_HEADERS, "Retry-After": "60"},
    )
