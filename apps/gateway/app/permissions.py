from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_access import lock_admin
from app.auth import require_admin
from app.database import Session, get_session
from app.models import MCPServer, ToolGrant, User
from app.registry import upstream_headers
from app.tools import release_auth_connection
from app.upstreams import UpstreamError, discover_tools


router = APIRouter(prefix="/api/users", tags=["tool permissions"])


class PermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def qualified_name(cls, value):
        slug, separator, tool = value.partition("__")
        if not separator or not slug or not tool or value != value.strip():
            raise ValueError("Use a tool name in server__tool format.")
        return value


async def member_account(session, user_id, *, lock=False):
    query = select(User).where(User.id == user_id)
    if lock:
        query = query.with_for_update()
    user = await session.scalar(query)
    if user is None or user.role != "member":
        raise HTTPException(404, "Member not found.")
    return user


def server_identity(server):
    return (
        server.slug,
        server.url,
        server.auth_header_name,
        server.credential_encrypted,
    )


@router.get("/{user_id}/permissions")
async def get_permissions(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await member_account(session, user_id)
    rows = (
        await session.execute(
            select(ToolGrant, MCPServer)
            .join(MCPServer, ToolGrant.server_id == MCPServer.id)
            .where(ToolGrant.user_id == user_id)
            .order_by(MCPServer.slug, ToolGrant.tool_name)
        )
    ).all()
    return {
        "user_id": user_id,
        "tools": [
            {
                "name": f"{server.slug}__{grant.tool_name}",
                "server_id": server.id,
                "server_enabled": server.enabled,
                "granted_by_id": grant.granted_by_id,
                "created_at": grant.created_at,
            }
            for grant, server in rows
        ],
    }


@router.post("/{user_id}/permissions", status_code=201)
async def grant_permission(
    user_id: int,
    body: PermissionBody,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    # Do not retain the authentication connection during MCP discovery.
    await release_auth_connection(admin, session)
    slug, _, tool_name = body.name.partition("__")

    async with Session() as database:
        member = await member_account(database, user_id)
        if not member.active:
            raise HTTPException(409, "Member account is inactive.")
        server = await database.scalar(
            select(MCPServer).where(MCPServer.slug == slug)
        )
        if server is None:
            raise HTTPException(404, "Server not found.")
        if not server.enabled:
            raise HTTPException(409, "Enable the server before granting access.")

    try:
        tools = await discover_tools(
            server.url,
            server_id=server.id,
            headers=upstream_headers(server),
            timeout_s=min(3.0, server.timeout_s),
        )
    except UpstreamError:
        raise HTTPException(
            503, "Cannot verify this tool while its server is unreachable."
        ) from None
    if tool_name not in {tool.name for tool in tools}:
        raise HTTPException(422, "This server does not advertise that tool.")

    async with Session() as database:
        # Discovery can outlast an account change. Recheck under the same gate
        # used by account disabling before taking member/server write locks.
        actor = await lock_admin(database, admin)
        member = await member_account(database, user_id, lock=True)
        if not member.active:
            raise HTTPException(409, "Member account is inactive.")
        current = await database.scalar(
            select(MCPServer)
            .where(MCPServer.id == server.id)
            .with_for_update()
        )
        if (
            current is None
            or not current.enabled
            or server_identity(current) != server_identity(server)
        ):
            raise HTTPException(409, "Server settings changed; refresh and retry.")
        await database.execute(
            insert(ToolGrant)
            .values(
                user_id=user_id,
                server_id=current.id,
                tool_name=tool_name,
                granted_by_id=actor.id,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "server_id", "tool_name"]
            )
        )
        await database.commit()
    return {"user_id": user_id, "name": body.name, "granted": True}


@router.delete("/{user_id}/permissions", status_code=204)
async def revoke_permission(
    user_id: int,
    body: PermissionBody,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    session.expunge(admin)
    await lock_admin(session, admin)
    await member_account(session, user_id, lock=True)
    slug, _, tool_name = body.name.partition("__")
    server = await session.scalar(
        select(MCPServer).where(MCPServer.slug == slug)
    )
    if server is None:
        raise HTTPException(404, "Server not found; refresh the permission list.")
    await session.execute(
        delete(ToolGrant).where(
            ToolGrant.user_id == user_id,
            ToolGrant.server_id == server.id,
            ToolGrant.tool_name == tool_name,
        )
    )
    await session.commit()
    return Response(status_code=204)
