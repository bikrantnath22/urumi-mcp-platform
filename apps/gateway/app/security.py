import asyncio
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet


JWT_SECRET = os.getenv("JWT_SECRET", "")
FERNET_KEY = os.getenv("FERNET_KEY", "")
APP_ORIGIN = os.getenv(
    "APP_ORIGIN", "http://app.localtest.me"
).rstrip("/")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_TTL = 8 * 60 * 60

if len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET must contain at least 32 characters.")

if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY must be set.")

cipher = Fernet(FERNET_KEY.encode())
hasher = PasswordHasher()
password_gate = asyncio.Semaphore(2)

DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


async def hash_password(password: str) -> str:
    async with password_gate:
        return await asyncio.to_thread(hasher.hash, password)


async def verify_password(password: str, stored_hash: str | None) -> bool:
    async with password_gate:
        try:
            valid = await asyncio.to_thread(
                hasher.verify,
                stored_hash or DUMMY_HASH,
                password,
            )
            return bool(valid and stored_hash)
        except (VerificationError, InvalidHashError):
            return False


def create_session_token(user_id: int, session_version: int = 0, *,
    session_id: str | None = None, expires_at: datetime | None = None) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user_id), "ver": session_version, "iat": now,
        "exp": expires_at or now + timedelta(seconds=SESSION_TTL),
        "iss": "urumi-gateway", "aud": "urumi-dashboard", "kind": "dashboard"}
    if session_id is not None:
        claims["sid"] = session_id
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def read_session_token(token: str, *, include_version: bool = False, include_session: bool = False):
    claims = jwt.decode(
        token,
        JWT_SECRET,
        algorithms=["HS256"],
        issuer="urumi-gateway",
        audience="urumi-dashboard",
        options={"require": ["sub", "iat", "exp", "iss", "aud", "kind"]},
    )
    if claims["kind"] != "dashboard":
        raise ValueError("Wrong token type.")

    user_id = int(claims["sub"])
    if user_id <= 0:
        raise ValueError("Invalid user ID.")

    version = claims.get("ver", 0)
    if type(version) is not int or version < 0:
        raise ValueError("Invalid session version.")
    if include_session:
        session_id = claims.get("sid")
        if not isinstance(session_id, str) or len(session_id) != 43:
            raise ValueError("Sign in again to create a server-backed session.")
        return user_id, version, session_id
    return (user_id, version) if include_version else user_id


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_api_key() -> str:
    return "urm_" + secrets.token_urlsafe(32)


def encrypt_credential(value: str) -> str:
    return cipher.encrypt(value.encode()).decode()


def decrypt_credential(value: str) -> str:
    return cipher.decrypt(value.encode()).decode()
