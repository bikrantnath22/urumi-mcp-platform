"""Queue authenticated store creation; Kubernetes work runs in the worker."""

import hashlib
import json
import os
import re
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import browser_sessions
from app.auth import current_user
from app.database import get_session
from app.models import SecurityEvent, Store, StoreOperation, User
from app.store_settings import get_store_settings, lock_store_admission
from app.stores import public_operation, public_store


router = APIRouter(prefix="/api/stores", tags=["stores"])
RESERVED_SLUGS = {"admin", "api", "app", "platform", "store", "www"}
SLUG_PATTERN = r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?"
DOMAIN_PATTERN = (
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


class StoreCreationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: UUID
    name: str = Field(strict=True, min_length=1, max_length=100)
    slug: str = Field(strict=True, min_length=1, max_length=32)

    @field_validator("name")
    @classmethod
    def readable_name(cls, value):
        if not value.isprintable():
            raise ValueError("Use a store name without control characters.")
        return value

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value):
        value = value.lower()
        if not re.fullmatch(SLUG_PATTERN, value) or value in RESERVED_SLUGS:
            raise ValueError(
                "Use 1-32 lowercase letters, numbers or internal hyphens. "
                "Choose an address that is not reserved."
            )
        return value


def request_fingerprint(body):
    payload = {
        "version": 1,
        "action": "provision",
        "name": body.name,
        "slug": body.slug,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def store_address(slug):
    domain = os.getenv("STORE_BASE_DOMAIN", "localtest.me").strip().lower()
    local = domain == "localtest.me" or domain.endswith(".localtest.me")
    scheme = os.getenv(
        "STORE_URL_SCHEME", "http" if local else "https",
    ).strip().lower()

    if (
        len(domain) > 200
        or not re.fullmatch(DOMAIN_PATTERN, domain)
        or scheme not in {"http", "https"}
        or (scheme == "http" and not local)
    ):
        raise HTTPException(503, "Store address configuration is unavailable.")

    return f"{scheme}://{slug}.{domain}"


@router.post("", status_code=202)
async def request_store_creation(
    body: StoreCreationBody,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"
    actor_id, version = user.id, user.session_version
    fingerprint = request_fingerprint(body)

    try:
        # Shared with admin setting edits; held only for this transaction.
        await lock_store_admission(session)

        actor = await session.scalar(
            select(User)
            .where(User.id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            actor is None
            or not actor.active
            or not actor.password_hash
            or actor.session_version != version
        ):
            raise HTTPException(401, "Account or login session is unavailable.")

        # Recheck revocation/expiry after waiting for mutation locks.
        await browser_sessions.load(
            session, actor, request.state.browser_session.id, lock=True,
        )

        previous = (
            await session.execute(
                select(Store, StoreOperation)
                .join(StoreOperation, StoreOperation.store_id == Store.id)
                .where(
                    StoreOperation.requested_by_id == actor.id,
                    StoreOperation.request_key == body.request_id,
                )
                .execution_options(populate_existing=True)
            )
        ).one_or_none()

        # A retry is checked before current quotas or the creation pause.
        if previous is not None:
            store, operation = previous

            if (
                operation.action != "provision"
                or operation.request_fingerprint != fingerprint
            ):
                raise HTTPException(
                    409, "This request ID was already used for another request.",
                )

            if store.status == "Deleted":
                raise HTTPException(
                    409, "The store from this request has already been deleted.",
                )

            result = {
                "store": public_store(store),
                "operation": public_operation(operation),
                "replayed": True,
            }
            await session.commit()
            response.status_code = 200
            response.headers["Location"] = f"/api/stores/{store.id}"
            return result

        settings = await get_store_settings(session, lock=True)

        if not settings.enabled:
            raise HTTPException(409, "New store creation is paused.")

        # Failed/Deleting stores may still hold resources; count until Deleted.
        total, owned = (
            await session.execute(
                select(
                    func.count(Store.id),
                    func.count(Store.id).filter(Store.owner_id == actor.id),
                ).where(Store.status != "Deleted")
            )
        ).one()

        if owned >= settings.max_per_user:
            raise HTTPException(409, "Your store limit has been reached.")

        if total >= settings.max_total:
            raise HTTPException(409, "The platform store limit has been reached.")

        existing_slug = await session.scalar(
            select(Store.id).where(Store.slug == body.slug)
        )
        if existing_slug is not None:
            raise HTTPException(
                409, "This store address is unavailable. Choose another slug.",
            )

        url = store_address(body.slug)
        now = await session.scalar(select(func.clock_timestamp()))
        store_id = uuid4()

        store = Store(
            id=store_id,
            owner_id=actor.id,
            name=body.name,
            slug=body.slug,
            namespace=f"urumi-store-{store_id.hex}",
            release_name="store",
            url=url,
            status="Provisioning",
            desired_state="present",
            generation=1,
            created_at=now,
            updated_at=now,
        )
        session.add(store)
        await session.flush()

        operation = StoreOperation(
            id=uuid4(),
            store_id=store.id,
            requested_by_id=actor.id,
            request_key=body.request_id,
            request_fingerprint=fingerprint,
            action="provision",
            store_generation=store.generation,
            status="Pending",
            attempts=0,
            available_at=now,
            deadline_at=now + timedelta(seconds=settings.timeout_seconds),
            created_at=now,
            updated_at=now,
        )
        session.add(operation)
        session.add(SecurityEvent(
            actor_id=actor.id,
            target_user_id=actor.id,
            action="store.creation_requested",
            details={
                "store_id": str(store.id),
                "operation_id": str(operation.id),
                "slug": store.slug,
            },
        ))
        await session.flush()

        result = {
            "store": public_store(store),
            "operation": public_operation(operation),
            "replayed": False,
        }
        await session.commit()
        response.headers["Location"] = f"/api/stores/{store.id}"
        return result

    except HTTPException:
        await session.rollback()
        raise

    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            409, "The store request conflicts with an existing record. "
            "Check Stores before retrying with the same request ID.",
        ) from None

    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(
            503, "Store requests are temporarily unavailable. "
            "Check Stores before retrying with the same request ID.",
        ) from None
