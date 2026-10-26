from starlette.routing import Route
from app.mcp_api import mcp_app, mcp_sessions
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.usage_limits import router as usage_limits_router
from app import rate_limits
from app.monitoring import router as monitoring_router
from app.observability import RequestDiagnostics
from app.catalog_cache import catalog_cache
from app.auth import router
from app.admin_access import router as admin_access_router
from app.servers import router as servers_router
from app.users import router as users_router
from app.permissions import router as permissions_router
from app.audit import router as audit_router
from app.tools import router as tools_router
from app.chat import router as chat_router
from app.chats import router as chats_router
from app.media import router as media_router
from app.registry import bootstrap_servers
from app.database import Session, engine
from app.models import User
from app.security import hash_password
from app.store_settings_api import router as store_settings_router
from app.stores import router as stores_router
from app.store_requests import router as store_requests_router
from app.store_lifecycle import router as store_lifecycle_router
from app.store_access import router as store_access_router
from app.store_wordpress_setup import router as store_wordpress_setup_router


async def bootstrap_admin():
    async with Session() as session:
        existing_user = await session.scalar(select(User.id).limit(1))
        if existing_user is not None:
            return

        email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
        password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")

        if "@" not in email or len(password) < 12:
            raise RuntimeError(
                "Set a bootstrap admin email and a password of at least 12 characters."
            )

        password_hash = await hash_password(password)
        await session.execute(
            insert(User)
            .values(
                email=email,
                password_hash=password_hash,
                role="admin",
                active=True,
            )
            .on_conflict_do_nothing(index_elements=["email"])
        )
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await bootstrap_admin()
        await bootstrap_servers()
        async with mcp_sessions.run():
            cleanup_task = asyncio.create_task(rate_limits.cleanup_loop())
            try:
                yield
            finally:
                await rate_limits.stop_cleanup(cleanup_task)
    finally:
        await catalog_cache.close()
        await engine.dispose()


app = FastAPI(
    title="Urumi Gateway",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(RequestDiagnostics)
app.include_router(monitoring_router)

app.add_exception_handler(rate_limits.RateLimitExceeded, rate_limits.limit_response)
app.add_exception_handler(rate_limits.LimitsUnavailable, rate_limits.unavailable_response)
app.include_router(usage_limits_router)
app.include_router(router)
app.include_router(admin_access_router)
app.include_router(servers_router)
app.include_router(users_router)
app.include_router(permissions_router)
app.include_router(audit_router)
app.include_router(tools_router)
app.include_router(chat_router)
app.include_router(chats_router)
app.include_router(media_router)
app.include_router(store_settings_router)
app.include_router(stores_router)
app.include_router(store_requests_router)
app.include_router(store_lifecycle_router)
app.include_router(store_access_router)
app.include_router(store_wordpress_setup_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception:
        return JSONResponse({"ready": False}, status_code=503)


app.router.routes.append(
    Route("/mcp", endpoint=mcp_app, include_in_schema=False)
)
