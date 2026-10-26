import re

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.database import get_session
from app.catalog_cache import catalog_cache
from app.models import MCPServer, ToolGrant, User
from app.managed_servers import (
    load_manual_server,
    lock_manual_registry,
    reject_reserved_identity,
)
from app.tools import release_auth_connection
from app.registry import list_servers, public_server, refresh_server
from app.security import encrypt_credential
from app.upstream_security import UpstreamPolicyError, destination_policy


router = APIRouter(
    prefix="/api/servers",
    tags=["servers"],
    dependencies=[Depends(require_admin)],
)

RESERVED_HEADERS = {
    "host",
    "cookie",
    "accept",
    "content-type",
    "content-length",
    "connection",
    "transfer-encoding",
    "proxy-authorization",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
}


class ServerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    timeout_s: float = Field(default=15, ge=1, le=60)
    auth_header_name: str | None = Field(default=None, max_length=100)
    credential: str | None = Field(default=None, max_length=4096)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Server name must not be blank.")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return str(destination_policy.validate_url(value).url)

    @field_validator("auth_header_name")
    @classmethod
    def validate_header(cls, value):
        if value is None or value == "":
            return None
        if (
            not re.fullmatch(r"[A-Za-z0-9-]+", value)
            or value.lower() in RESERVED_HEADERS
        ):
            raise ValueError("Credential header is invalid or reserved.")
        return value

    @field_validator("credential")
    @classmethod
    def validate_credential(cls, value):
        if value is None or value == "":
            return None
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise ValueError("Credential must contain printable ASCII only.")
        return value


def apply_credentials(server: MCPServer, body: ServerBody):
    if "auth_header_name" in body.model_fields_set:
        server.auth_header_name = body.auth_header_name

    if "credential" in body.model_fields_set:
        if body.credential is None:
            server.credential_encrypted = None
            server.auth_header_name = None
        else:
            server.credential_encrypted = encrypt_credential(body.credential)

    if bool(server.auth_header_name) != bool(server.credential_encrypted):
        raise HTTPException(
            422,
            "Provide both a credential header name and its value.",
        )


async def save_server(session: AsyncSession):
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "A server with this slug already exists.") from None


async def validate_destination(url: str):
    try:
        await destination_policy.prepare(url)
    except UpstreamPolicyError as error:
        raise HTTPException(422, str(error)) from None


@router.get("")
async def get_servers(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await release_auth_connection(admin, session)
    return await list_servers()


@router.post("", status_code=201)
async def create_server(
    body: ServerBody,
    session: AsyncSession = Depends(get_session),
):
    reject_reserved_identity(body.slug, body.url)
    await validate_destination(body.url)
    await lock_manual_registry(session)
    server = MCPServer(
        slug=body.slug,
        name=body.name,
        url=body.url,
        enabled=body.enabled,
        timeout_s=body.timeout_s,
        status="Unreachable" if body.enabled else "Disabled",
        status_reason="Not checked yet." if body.enabled else None,
    )
    apply_credentials(server, body)
    session.add(server)
    await save_server(session)
    catalog_cache.invalidate(server.id)
    return public_server(server)


@router.put("/{server_id}")
async def edit_server(
    server_id: int,
    body: ServerBody,
    session: AsyncSession = Depends(get_session),
):
    reject_reserved_identity(body.slug, body.url)
    server = await load_manual_server(session, server_id, lock=False)

    expected_url = server.url
    if body.url != expected_url:
        await validate_destination(body.url)

    # Recheck ownership under the worker's lock, then lock the server row.
    server = await load_manual_server(session, server_id, lock=True)
    if server.url != expected_url:
        raise HTTPException(409, "Server settings changed. Refresh before saving.")
    before = (
        server.slug,
        server.url,
        server.enabled,
        server.timeout_s,
        server.auth_header_name,
        server.credential_encrypted,
    )

    server.slug = body.slug
    server.name = body.name
    server.url = body.url
    server.enabled = body.enabled
    server.timeout_s = body.timeout_s
    apply_credentials(server, body)

    after = (
        server.slug,
        server.url,
        server.enabled,
        server.timeout_s,
        server.auth_header_name,
        server.credential_encrypted,
    )

    # Grants must not silently transfer to a different upstream identity.
    if before[1] != after[1] or before[4:] != after[4:]:
        await session.execute(
            delete(ToolGrant).where(ToolGrant.server_id == server.id)
        )

    if before != after:
        server.status = "Unreachable" if server.enabled else "Disabled"
        server.status_reason = "Settings changed; check required." if server.enabled else None
        server.last_checked_at = None

    await save_server(session)
    catalog_cache.invalidate(server.id)
    return public_server(server)


@router.delete("/{server_id}", status_code=204)
async def delete_server(
    server_id: int,
    session: AsyncSession = Depends(get_session),
):
    server = await load_manual_server(session, server_id, lock=True)
    await session.delete(server)
    await session.commit()
    catalog_cache.invalidate(server_id)
    return Response(status_code=204)


@router.post("/{server_id}/check")
async def check_server(
    server_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await release_auth_connection(admin, session)
    result = await refresh_server(server_id)
    if result is None:
        raise HTTPException(404, "Server not found.")
    return result
