import copy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.rate_limits import enforce_chat
from app.chat_context import previous_context
from app.database import Session
from app.models import ChatConversation, ChatTurn


def public_conversation(conversation):
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


def safe_response(data):
    """Keep readable tool activity without storing binary image payloads."""
    result = copy.deepcopy(data)
    for call in result.get("tool_calls", []):
        arguments = call.get("arguments") or {}
        if "content_base64" in arguments:
            arguments["content_base64"] = "[image data omitted]"
        tool_result = call.get("result") or {}
        for content in tool_result.get("content", []):
            if content.get("type") in {"image", "audio"}:
                content.pop("data", None)
                content["omitted"] = True
    return result


def public_turn(turn):
    result = turn.response or {}
    error = result.get("error") or {}
    return {
        "id": str(turn.id),
        "question": turn.message,
        "image_id": turn.image_id,
        "image_name": turn.image_name,
        "image_tool": result.get("image_tool", ""),
        "created_at": turn.created_at.isoformat(),
        "reply": result.get("reply") or "",
        "error": error.get("message", "") if isinstance(error, dict) else str(error),
        "tool_calls": result.get("tool_calls", []),
        "duration_ms": result.get("duration_ms"),
        "retry_after_s": error.get("retry_after_s") if isinstance(error, dict) else None,
        "retry_at": error.get("retry_at") if isinstance(error, dict) else None,
        "error_provider": error.get("provider") if isinstance(error, dict) else None,
        "results_complete": error.get("results_complete") is True if isinstance(error, dict) else False,
        "limit_reason": error.get("reason") if isinstance(error, dict) else None,
        "model_requests": result.get("model_requests", []),
        "pending": turn.status == "running",
        "phase": "chat",
        "status": turn.status,
    }


async def expire_requests(session, conversation_id):
    now = datetime.now(timezone.utc)
    expired = list(await session.scalars(
        select(ChatTurn)
        .where(
            ChatTurn.conversation_id == conversation_id,
            ChatTurn.status == "running",
            ChatTurn.deadline_at <= now,
        )
        .with_for_update()
    ))
    for turn in expired:
        response = copy.deepcopy(turn.response or {})
        for call in response.get("tool_calls", []):
            if call.get("status") == "pending":
                call["status"] = "unknown"
        response["error"] = {
            "message": "This request was interrupted. Check recorded tool activity and the audit log before repeating changes.",
            "automatic_retry_allowed": False,
        }
        response["conversation_id"] = str(conversation_id)
        response["turn_id"] = str(turn.id)
        turn.response = response
        turn.status = "interrupted"
        turn.http_status = 503




async def begin_turn(user_id, conversation_id, request_id, message, image_id, image_name, image_tool=""):
    try:
        async with Session() as session:
            prior = (await session.execute(
                select(ChatTurn.conversation_id, ChatConversation.user_id)
                .join(ChatConversation, ChatTurn.conversation_id == ChatConversation.id)
                .where(ChatTurn.id == request_id)
            )).first()
            if prior:
                if prior.user_id != user_id:
                    raise HTTPException(404, "Chat request not found.")
                if conversation_id is not None and conversation_id != prior.conversation_id:
                    raise HTTPException(409, "This request ID was used in a different chat.")
                conversation_id = prior.conversation_id

            if conversation_id is None:
                conversation = ChatConversation(id=uuid4(), user_id=user_id, title=" ".join(message.split())[:80])
                session.add(conversation)
                await session.flush()
            else:
                conversation = await session.scalar(
                    select(ChatConversation)
                    .where(ChatConversation.id == conversation_id, ChatConversation.user_id == user_id, ChatConversation.deleted_at.is_(None))
                    .with_for_update()
                )
                if conversation is None:
                    raise HTTPException(404, "Chat not found.")

            await expire_requests(session, conversation.id)
            existing = await session.get(ChatTurn, request_id, populate_existing=True)
            if existing is not None:
                if ((existing.message, existing.image_id, existing.image_name)
                    != (message, image_id, image_name)
                    or (existing.response or {}).get("image_tool", "") != image_tool):
                    raise HTTPException(409, "This request ID was already used for different input.")
                await session.commit()
                if existing.status == "running":
                    raise HTTPException(409, "This request is still running. Refresh the saved chat.")
                return {"cached": True, "response": existing.response, "http_status": existing.http_status or 200}

            running = await session.scalar(select(ChatTurn.id).where(
                ChatTurn.conversation_id == conversation.id, ChatTurn.status == "running"
            ).limit(1))
            if running is not None:
                # Persist expired entries before rejecting a different active turn.
                await session.commit()
                raise HTTPException(409, "This chat already has a request in progress. Wait or open a new chat.")
            await enforce_chat(session, user_id)
            earlier = list(await session.scalars(
                select(ChatTurn)
                .where(ChatTurn.conversation_id == conversation.id)
                .order_by(ChatTurn.created_at.desc(), ChatTurn.id.desc())
                .limit(3)
            ))
            now = datetime.now(timezone.utc)
            session.add(ChatTurn(
                id=request_id,
                conversation_id=conversation.id,
                message=message,
                image_id=image_id,
                image_name=image_name,
                status="running",
                deadline_at=now + timedelta(seconds=120),
                response={"image_tool": image_tool} if image_tool else {},
            ))
            conversation.updated_at = now
            await session.commit()
            return {"cached": False, "conversation_id": conversation.id, "turn_id": request_id,
                    "history": previous_context(earlier, message)}
    except IntegrityError:
        # A racing duplicate is rejected instead of performing actions twice.
        raise HTTPException(409, "This request is already registered. Refresh the saved chat.") from None


async def save_progress(user_id, turn_id, response):
    async with Session() as session:
        turn = await session.scalar(
            select(ChatTurn)
            .join(ChatConversation, ChatTurn.conversation_id == ChatConversation.id)
            .where(ChatTurn.id == turn_id, ChatConversation.user_id == user_id, ChatConversation.deleted_at.is_(None))
            .with_for_update(of=ChatTurn)
        )
        if turn is None:
            raise RuntimeError("Chat request is unavailable.")
        if turn.status == "running":
            turn.response = safe_response(response)
            await session.commit()


async def finish_turn(user_id, turn_id, response, http_status):
    async with Session() as session:
        # Lock parent before child, matching begin_turn's lock order.
        conversation = await session.scalar(
            select(ChatConversation)
            .join(ChatTurn, ChatTurn.conversation_id == ChatConversation.id)
            .where(ChatTurn.id == turn_id, ChatConversation.user_id == user_id, ChatConversation.deleted_at.is_(None))
            .with_for_update(of=ChatConversation)
        )
        if conversation is None:
            raise RuntimeError("Chat is unavailable.")
        turn = await session.get(ChatTurn, turn_id)
        if turn is None or turn.status != "running":
            raise RuntimeError("Chat request is no longer running.")
        turn.response = safe_response(response)
        turn.status = "failed" if response.get("error") else "completed"
        turn.http_status = http_status
        conversation.updated_at = datetime.now(timezone.utc)
        await session.commit()
