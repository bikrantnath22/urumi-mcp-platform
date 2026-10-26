from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.core import execute_gateway_tool, list_gateway_tools
from app.database import get_session
from app.models import User


router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    arguments: dict = Field(default_factory=dict)


async def release_auth_connection(user: User, session: AsyncSession):
    # Keep the loaded user identity available after ending the read transaction.
    session.expunge(user)
    await session.rollback()


@router.get("")
async def get_tools(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await release_auth_connection(user, session)
    tools = await list_gateway_tools(user)
    return {
        "tools": [
            tool.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            for tool in tools
        ]
    }


@router.post("/call")
async def execute_tool(
    body: ToolCallBody,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    await release_auth_connection(user, session)

    result = await execute_gateway_tool(
        user,
        body.name,
        body.arguments,
        source="dashboard",
    )
    return result.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
