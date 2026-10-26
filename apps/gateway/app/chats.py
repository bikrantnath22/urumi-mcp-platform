from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.chat_store import expire_requests, public_conversation, public_turn
from app.database import get_session
from app.models import ChatConversation, ChatTurn, User


router = APIRouter(prefix="/api/chats", tags=["saved chats"])


class CreateChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    title: str = Field(min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Chat title must not be blank.")
        return value


@router.post("", status_code=201)
async def create_chat(
    body: CreateChatBody,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    existing = await session.get(ChatConversation, body.id)
    if existing is not None:
        if existing.user_id != user.id or existing.deleted_at is not None:
            raise HTTPException(404, "Chat not found.")
        return public_conversation(existing)
    conversation = ChatConversation(id=body.id, title=body.title, user_id=user.id)
    session.add(conversation)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Chat creation is already registered. Refresh saved chats.") from None
    return public_conversation(conversation)


@router.get("")
async def list_chats(
    limit: int = Query(default=20, ge=1, le=50),
    before_id: UUID | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(ChatConversation).where(ChatConversation.user_id == user.id, ChatConversation.deleted_at.is_(None))
    if before_id is not None:
        anchor = await session.scalar(select(ChatConversation).where(
            ChatConversation.id == before_id, ChatConversation.user_id == user.id, ChatConversation.deleted_at.is_(None)
        ))
        if anchor is None:
            raise HTTPException(404, "Chat cursor not found.")
        query = query.where(tuple_(ChatConversation.created_at, ChatConversation.id)
                            < tuple_(anchor.created_at, anchor.id))
    rows = list(await session.scalars(query.order_by(
        ChatConversation.created_at.desc(), ChatConversation.id.desc()
    ).limit(limit + 1)))
    has_more = len(rows) > limit
    items = rows[:limit]
    return {"items": [public_conversation(item) for item in items],
            "next_before_id": str(items[-1].id) if has_more else None}


@router.get("/{conversation_id}")
async def get_chat(
    conversation_id: UUID,
    limit: int = Query(default=20, ge=1, le=50),
    before_id: UUID | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    conversation = await session.scalar(select(ChatConversation).where(
        ChatConversation.id == conversation_id, ChatConversation.user_id == user.id, ChatConversation.deleted_at.is_(None)
    ))
    if conversation is None:
        raise HTTPException(404, "Chat not found.")
    await expire_requests(session, conversation.id)
    await session.commit()
    query = select(ChatTurn).where(ChatTurn.conversation_id == conversation.id)
    if before_id is not None:
        anchor = await session.scalar(select(ChatTurn).where(
            ChatTurn.id == before_id, ChatTurn.conversation_id == conversation.id
        ))
        if anchor is None:
            raise HTTPException(404, "Message cursor not found.")
        query = query.where(tuple_(ChatTurn.created_at, ChatTurn.id)
                            < tuple_(anchor.created_at, anchor.id))
    rows = list(await session.scalars(query.order_by(
        ChatTurn.created_at.desc(), ChatTurn.id.desc()
    ).limit(limit + 1)))
    has_more = len(rows) > limit
    items = rows[:limit]
    return {"conversation": public_conversation(conversation),
            "turns": [public_turn(item) for item in reversed(items)],
            "next_before_id": str(items[-1].id) if has_more else None}


@router.delete("/{conversation_id}", status_code=204)
async def delete_chat(
    conversation_id: UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    # Same parent lock as begin_turn/finish_turn; deletion cannot race a new turn.
    conversation = await session.scalar(select(ChatConversation).where(
        ChatConversation.id == conversation_id, ChatConversation.user_id == user.id
    ).with_for_update())
    if conversation is None:
        raise HTTPException(404, "Chat not found.")
    if conversation.deleted_at is not None:
        return Response(status_code=204)
    await expire_requests(session, conversation.id)
    running = await session.scalar(select(ChatTurn.id).where(
        ChatTurn.conversation_id == conversation.id, ChatTurn.status == "running"
    ).limit(1))
    if running is not None:
        raise HTTPException(409, "Wait for the running request to finish before deleting this chat.")
    # Erase personal content but keep IDs so a replay cannot repeat prior actions.
    await session.execute(update(ChatTurn).where(
        ChatTurn.conversation_id == conversation.id
    ).values(message="", image_id=None, image_name="", response={},
             status="interrupted", http_status=410))
    now = datetime.now(timezone.utc)
    conversation.title = "Deleted conversation"
    conversation.deleted_at = now
    conversation.updated_at = now
    await session.commit()
    return Response(status_code=204)
