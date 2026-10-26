"""Browser lifecycle requests; external cleanup stays in the private worker."""

import hashlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import browser_sessions
from app.auth import current_user
from app.database import get_session
from app.models import SecurityEvent, Store, StoreOperation, User
from app.store_completion import StoreCompletionConflict, _registrations
from app.store_settings import get_store_settings, lock_store_admission
from app.store_registration import definitions_for
from app.stores import public_operation, public_store

router = APIRouter(prefix="/api/stores", tags=["stores"])


class DeleteStoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    generation: int = Field(strict=True, ge=1)


class StoreAccessBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    enabled: bool


async def _actor(session, request, user, *, admin=False):
    actor_id, version = user.id, user.session_version
    await lock_store_admission(session)
    actor = await session.scalar(
        select(User).where(User.id == actor_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if (actor is None or not actor.active or not actor.password_hash
            or actor.session_version != version):
        raise HTTPException(401, "Account or login session is unavailable.")
    if admin and actor.role != "admin":
        raise HTTPException(403, "Admin access is required.")
    await browser_sessions.load(
        session, actor, request.state.browser_session.id, lock=True,
    )
    return actor


async def _store(session, actor, store_id):
    statement = select(Store).where(Store.id == store_id)
    if actor.role != "admin":
        statement = statement.where(Store.owner_id == actor.id)
    store = await session.scalar(
        statement.with_for_update().execution_options(populate_existing=True)
    )
    if store is None:
        raise HTTPException(404, "Store not found.")
    return store


async def _servers(session, store, *, both):
    claim = SimpleNamespace(
        namespace=store.namespace, release_name=store.release_name,
        owner_id=store.owner_id, url=store.url,
    )
    servers = await _registrations(session, store, claim, require_both=both)
    if both:
        claim.store_id, claim.name, claim.slug = store.id, store.name, store.slug
        expected = {item.url: item for item in definitions_for(claim)}
        for server in servers:
            definition = expected.get(server.url)
            if (definition is None or server.slug != definition.slug
                    or server.name != definition.name or server.timeout_s != 15
                    or server.auth_header_name is not None
                    or server.credential_encrypted is not None):
                raise StoreCompletionConflict("Managed MCP registration changed.")
    return servers


def _disable(servers):
    for server in servers:
        server.enabled = False
        server.status = "Disabled"
        server.status_reason = None


@router.delete("/{store_id}", status_code=202)
async def delete_store(
    store_id: UUID, body: DeleteStoreBody, request: Request, response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"
    fingerprint = hashlib.sha256(
        f"delete:v1:{store_id}:{body.generation}".encode("ascii")
    ).hexdigest()
    try:
        actor = await _actor(session, request, user)
        previous = await session.scalar(
            select(StoreOperation).where(
                StoreOperation.requested_by_id == actor.id,
                StoreOperation.request_key == body.request_id,
            )
        )
        store = await _store(session, actor, store_id)
        if previous is not None:
            if (previous.store_id != store.id or previous.action != "delete"
                    or previous.request_fingerprint != fingerprint):
                raise HTTPException(409, "This request ID was used for another request.")
            result = {
                "store": public_store(store), "operation": public_operation(previous),
                "replayed": True,
            }
            await session.commit()
            response.status_code = 200
            return result
        if store.status == "Deleted":
            raise HTTPException(404, "Store not found.")
        if store.status not in ("Ready", "Failed") or store.desired_state != "present":
            raise HTTPException(409, "Wait for the current store operation to finish.")
        if store.generation != body.generation:
            raise HTTPException(409, "Store information changed. Refresh before deleting.")
        active = await session.scalar(select(StoreOperation.id).where(
            StoreOperation.store_id == store.id,
            StoreOperation.status.in_(("Pending", "Running")),
        ))
        if active is not None:
            raise HTTPException(409, "A store operation is already in progress.")
        servers = await _servers(session, store, both=store.status == "Ready")
        settings = await get_store_settings(session, lock=True)
        now = await session.scalar(select(func.clock_timestamp()))
        _disable(servers)
        store.access_enabled = False
        store.access_revision += 1
        store.generation += 1
        store.desired_state = "absent"
        store.status = "Deleting"
        store.status_reason = None
        store.updated_at = now
        operation = StoreOperation(
            id=uuid4(), store_id=store.id, requested_by_id=actor.id,
            request_key=body.request_id, request_fingerprint=fingerprint,
            action="delete", store_generation=store.generation,
            status="Pending", attempts=0, available_at=now,
            deadline_at=now + timedelta(seconds=settings.timeout_seconds),
            created_at=now, updated_at=now,
        )
        session.add(operation)
        session.add(SecurityEvent(
            actor_id=actor.id, target_user_id=store.owner_id,
            action="store.deletion_requested",
            details={"store_id": str(store.id), "operation_id": str(operation.id)},
        ))
        await session.flush()
        result = {
            "store": public_store(store), "operation": public_operation(operation),
            "replayed": False,
        }
        await session.commit()
        response.headers["Location"] = f"/api/stores/{store.id}"
        return result
    except (HTTPException, StoreCompletionConflict) as error:
        await session.rollback()
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(409, "Store registrations changed. Refresh before retrying.") from None
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "A store request already exists. Refresh before retrying.") from None
    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(503, "Deletion could not be confirmed. Retry the saved request.") from None


@router.put("/{store_id}/access")
async def set_store_access(
    store_id: UUID, body: StoreAccessBody, request: Request, response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        actor = await _actor(session, request, user, admin=True)
        store = await _store(session, actor, store_id)
        if (store.status != "Ready" or store.desired_state != "present"
                or store.deleted_at is not None):
            raise HTTPException(409, "Only a Ready store can be enabled or disabled.")
        if not store.access_guarded:
            raise HTTPException(409, "The store maintenance gate must be installed first.")
        if store.generation != body.generation:
            raise HTTPException(409, "Store information changed. Refresh before saving.")
        # An uncertain reply can safely repeat the same requested state.
        if store.access_enabled != body.enabled:
            if store.access_revision != body.revision:
                raise HTTPException(409, "Another admin changed this store. Refresh first.")
            active = await session.scalar(select(StoreOperation.id).where(
                StoreOperation.store_id == store.id,
                StoreOperation.status.in_(("Pending", "Running")),
            ))
            if active is not None:
                raise HTTPException(409, "A store operation is already in progress.")
            servers = await _servers(session, store, both=True)
            for server in servers:
                if server.enabled != store.access_enabled:
                    raise StoreCompletionConflict("Store access registrations changed.")
            for server in servers:
                server.enabled = body.enabled
                server.status = "Unreachable" if body.enabled else "Disabled"
                server.status_reason = None
            store.access_enabled = body.enabled
            store.access_revision += 1
            store.updated_at = await session.scalar(select(func.clock_timestamp()))
            session.add(SecurityEvent(
                actor_id=actor.id, target_user_id=store.owner_id,
                action="store.enabled" if body.enabled else "store.disabled",
                details={"store_id": str(store.id), "revision": store.access_revision},
            ))
        await session.flush()
        result = {"store": public_store(store)}
        await session.commit()
        return result
    except (HTTPException, StoreCompletionConflict) as error:
        await session.rollback()
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(409, "Store registrations changed. Refresh before retrying.") from None
    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(503, "Store access could not be confirmed. Refresh before retrying.") from None
