from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'member')",
            name="ck_users_role",
        ),
        CheckConstraint("session_version >= 0", name="ck_users_session_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(
        String(254), unique=True, nullable=False
    )
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(
        String(16), default="member", nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    invite_token_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    invite_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class APIKey(TimestampMixin, Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    key_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MCPServer(TimestampMixin, Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Connected', 'Unreachable', 'Disabled')",
            name="ck_servers_status",
        ),
        CheckConstraint(
            "timeout_s >= 1 AND timeout_s <= 60",
            name="ck_servers_timeout",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_header_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    credential_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    timeout_s: Mapped[float] = mapped_column(
        Float, default=15.0, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="Unreachable", nullable=False
    )
    status_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'error', 'denied')",
            name="ck_audit_outcome",
        ),
        CheckConstraint(
            "source IN ('mcp', 'chat', 'dashboard')",
            name="ck_audit_source",
        ),
        CheckConstraint(
            "duration_ms >= 0",
            name="ck_audit_duration",
        ),
        Index("ix_audit_user_created", "user_id", "created_at"),
        Index("ix_audit_server_created", "server_slug", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="SET NULL"),
        nullable=True,
    )
    server_slug: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    tool: Mapped[str] = mapped_column(String(256), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RegistryBootstrap(Base):
    __tablename__ = "registry_bootstrap"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_registry_bootstrap_id"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,
    )


class ToolGrant(TimestampMixin, Base):
    __tablename__ = "tool_grants"
    __table_args__ = (
        CheckConstraint("length(tool_name) > 0", name="ck_tool_grants_name"),
        Index("ix_tool_grants_server_id", "server_id"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True
    )
    tool_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    granted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ChatConversation(TimestampMixin, Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index("ix_chat_conversations_user_created", "user_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatTurn(TimestampMixin, Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'failed', 'interrupted')", name="ck_chat_turns_status"),
        Index("ix_chat_turns_conversation_created", "conversation_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    image_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    image_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SecurityEvent(TimestampMixin, Base):
    __tablename__ = "security_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class BrowserSession(TimestampMixin, Base):
    __tablename__ = "browser_sessions"
    __table_args__ = (
        CheckConstraint("session_version >= 0", name="ck_browser_sessions_version"),
        CheckConstraint("idle_expires_at <= absolute_expires_at", name="ck_browser_sessions_expiry"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageLimitSettings(Base):
    __tablename__ = "usage_limit_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_usage_limits_singleton"),
        CheckConstraint("revision >= 1", name="ck_usage_limits_revision"),
        CheckConstraint("login_account_limit >= 1 AND login_ip_limit >= login_account_limit AND login_window_seconds >= 60", name="ck_usage_limits_login"),
        CheckConstraint("chat_per_minute >= 1 AND chat_per_hour >= chat_per_minute AND chat_global_per_minute >= 1 AND tool_per_minute >= 1", name="ck_usage_limits_requests"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
    login_account_limit: Mapped[int] = mapped_column(Integer, server_default=text("5"), nullable=False)
    login_ip_limit: Mapped[int] = mapped_column(Integer, server_default=text("30"), nullable=False)
    login_window_seconds: Mapped[int] = mapped_column(Integer, server_default=text("600"), nullable=False)
    chat_per_minute: Mapped[int] = mapped_column(Integer, server_default=text("10"), nullable=False)
    chat_per_hour: Mapped[int] = mapped_column(Integer, server_default=text("100"), nullable=False)
    chat_global_per_minute: Mapped[int] = mapped_column(Integer, server_default=text("30"), nullable=False)
    tool_per_minute: Mapped[int] = mapped_column(Integer, server_default=text("60"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (CheckConstraint("used >= 1", name="ck_rate_limit_used"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)

class Store(TimestampMixin, Base):
    __tablename__ = "stores"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Provisioning', 'Ready', 'Failed', 'Deleting', 'Deleted')",
            name="ck_stores_status",
        ),
        CheckConstraint(
            "desired_state IN ('present', 'absent')",
            name="ck_stores_desired_state",
        ),
        UniqueConstraint(
            "analytics_mcp_server_id",
            name="uq_stores_analytics_mcp_server_id",
        ),
        CheckConstraint(
            "mcp_server_id IS NULL OR analytics_mcp_server_id IS NULL "
            "OR mcp_server_id <> analytics_mcp_server_id",
            name="ck_stores_distinct_mcp_servers",
        ),
        CheckConstraint("generation >= 1", name="ck_stores_generation"),
        CheckConstraint("access_revision >= 1", name="ck_stores_access_revision"),
        UniqueConstraint(
            "id", "namespace_uid", name="uq_stores_id_namespace_uid",
        ),
        UniqueConstraint(
            "namespace_uid",
            name="uq_stores_namespace_uid",
        ),
        CheckConstraint(
            "namespace_uid IS NULL OR length(namespace_uid) BETWEEN 1 AND 256",
            name="ck_stores_namespace_uid_length",
        ),
        UniqueConstraint(
            "credentials_secret_uid",
            name="uq_stores_credentials_secret_uid",
        ),
        CheckConstraint(
            "credentials_secret_uid IS NULL "
            "OR length(credentials_secret_uid) BETWEEN 1 AND 256",
            name="ck_stores_credentials_secret_uid_length",
        ),
        CheckConstraint(
            "credentials_secret_uid IS NULL OR namespace_uid IS NOT NULL",
            name="ck_stores_credentials_namespace",
        ),
        Index("ix_stores_owner_status", "owner_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid4
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False
    )
    namespace: Mapped[str] = mapped_column(
        String(63), unique=True, nullable=False
    )
    namespace_uid: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    credentials_secret_uid: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    release_name: Mapped[str] = mapped_column(
        String(53),
        default="store",
        server_default=text("'store'"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(
        Text, unique=True, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="Provisioning",
        server_default=text("'Provisioning'"),
        nullable=False,
    )
    access_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    access_guarded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    access_revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    desired_state: Mapped[str] = mapped_column(
        String(8),
        default="present",
        server_default=text("'present'"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )
    status_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    mcp_server_id: Mapped[int | None] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="SET NULL"),
        unique=True,
        nullable=True,
    )
    analytics_mcp_server_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "mcp_servers.id",
            name="fk_stores_analytics_mcp_server_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StoreOperation(TimestampMixin, Base):
    __tablename__ = "store_operations"
    __table_args__ = (
        CheckConstraint(
            "action IN ('provision', 'delete')",
            name="ck_store_operations_action",
        ),
        CheckConstraint(
            "status IN ('Pending', 'Running', 'Succeeded', 'Failed', 'Cancelled')",
            name="ck_store_operations_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND store_generation >= 1",
            name="ck_store_operations_counters",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_store_operations_fingerprint",
        ),
        CheckConstraint(
            "(status = 'Running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'Running' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_store_operations_lease",
        ),
        Index(
            "uq_store_operations_request",
            "requested_by_id",
            "request_key",
            unique=True,
        ),
        Index(
            "uq_store_operations_active",
            "store_id",
            unique=True,
            postgresql_where=text(
                "status IN ('Pending', 'Running')"
            ),
        ),
        Index(
            "ix_store_operations_queue",
            "status",
            "available_at",
            "created_at",
        ),
        Index(
            "ix_store_operations_lease",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid4
    )
    store_id: Mapped[UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_key: Mapped[UUID] = mapped_column(
        Uuid(), nullable=False
    )
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    store_generation: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="Pending",
        server_default=text("'Pending'"),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_token: Mapped[UUID | None] = mapped_column(
        Uuid(), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

class StoreProvisioningSettings(Base):
    __tablename__ = "store_provisioning_settings"
    __table_args__ = (
        CheckConstraint(
            "id = 1",
            name="ck_store_settings_singleton",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_store_settings_revision",
        ),
        CheckConstraint(
            "max_per_user >= 1 "
            "AND max_per_user <= max_total "
            "AND max_total <= 100",
            name="ck_store_settings_limits",
        ),
        CheckConstraint(
            "max_concurrent_creations >= 1 "
            "AND max_concurrent_creations <= 100",
            name="ck_store_settings_concurrency",
        ),
        CheckConstraint(
            "timeout_seconds >= 60 "
            "AND timeout_seconds <= 3600 "
            "AND timeout_seconds % 60 = 0",
            name="ck_store_settings_timeout",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    max_per_user: Mapped[int] = mapped_column(
        Integer,
        default=2,
        server_default=text("2"),
        nullable=False,
    )
    max_total: Mapped[int] = mapped_column(
        Integer,
        default=2,
        server_default=text("2"),
        nullable=False,
    )
    max_concurrent_creations: Mapped[int] = mapped_column(
        Integer,
        default=2,
        server_default=text("2"),
        nullable=False,
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        default=1200,
        server_default=text("1200"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class StoreVolume(TimestampMixin, Base):
    """Storage identities survive logical store deletion for safe recovery."""

    __tablename__ = "store_volumes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["store_id", "namespace_uid"],
            ["stores.id", "stores.namespace_uid"],
            name="fk_store_volumes_namespace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("claim_uid", name="uq_store_volumes_claim_uid"),
        UniqueConstraint("volume_name", name="uq_store_volumes_volume_name"),
        UniqueConstraint("volume_uid", name="uq_store_volumes_volume_uid"),
        CheckConstraint(
            "claim_name IN ('data-mysql-0', 'wordpress-data')",
            name="ck_store_volumes_claim_name",
        ),
        CheckConstraint(
            "length(namespace_uid) BETWEEN 1 AND 256 "
            "AND length(claim_uid) BETWEEN 1 AND 256",
            name="ck_store_volumes_claim_uids",
        ),
        CheckConstraint(
            "volume_name IS NULL OR length(volume_name) BETWEEN 1 AND 253",
            name="ck_store_volumes_volume_name",
        ),
        CheckConstraint(
            "volume_uid IS NULL OR length(volume_uid) BETWEEN 1 AND 256",
            name="ck_store_volumes_volume_uid",
        ),
        CheckConstraint(
            "volume_uid IS NULL OR volume_name IS NOT NULL",
            name="ck_store_volumes_volume_pair",
        ),
        CheckConstraint(
            "storage_class = 'local-path'",
            name="ck_store_volumes_storage_class",
        ),
    )

    store_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    claim_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    namespace_uid: Mapped[str] = mapped_column(String(256), nullable=False)
    claim_uid: Mapped[str] = mapped_column(String(256), nullable=False)
    volume_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
    volume_uid: Mapped[str | None] = mapped_column(String(256), nullable=True)
    storage_class: Mapped[str] = mapped_column(
        String(63), server_default=text("'local-path'"), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

class StoreWordPressSetup(Base):
    """One outstanding, hashed setup capability per store; never a password."""
    __tablename__ = "store_wordpress_setup"
    __table_args__ = (
        CheckConstraint("generation >= 1 AND access_revision >= 1 AND owner_id >= 1", name="ck_wp_setup_versions"),
        CheckConstraint("length(token_hash) = 64", name="ck_wp_setup_hash"),
    )
    store_id: Mapped[UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    access_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    namespace_uid: Mapped[str] = mapped_column(String(256), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
