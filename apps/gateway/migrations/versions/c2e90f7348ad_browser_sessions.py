"""Add persistent dashboard sessions with idle and absolute expiry."""
from alembic import op
import sqlalchemy as sa

revision = "c2e90f7348ad"
down_revision = "b8d16a29e504"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("browser_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("session_version >= 0", name="ck_browser_sessions_version"),
        sa.CheckConstraint("idle_expires_at <= absolute_expires_at", name="ck_browser_sessions_expiry"))
    op.create_index("ix_browser_sessions_user_id", "browser_sessions", ["user_id"])
    op.create_index("ix_browser_sessions_absolute_expires_at", "browser_sessions", ["absolute_expires_at"])


def downgrade():
    op.drop_table("browser_sessions")
