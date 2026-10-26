"""Add admin-managed request limits and shared quota counters."""
from alembic import op
import sqlalchemy as sa

revision = "d4e91b70a6f2"
down_revision = "c2e90f7348ad"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("usage_limit_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("login_account_limit", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("login_ip_limit", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("login_window_seconds", sa.Integer(), server_default=sa.text("600"), nullable=False),
        sa.Column("chat_per_minute", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("chat_per_hour", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("chat_global_per_minute", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("tool_per_minute", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_usage_limits_singleton"),
        sa.CheckConstraint("revision >= 1", name="ck_usage_limits_revision"),
        sa.CheckConstraint("login_account_limit >= 1 AND login_ip_limit >= login_account_limit AND login_window_seconds >= 60", name="ck_usage_limits_login"),
        sa.CheckConstraint("chat_per_minute >= 1 AND chat_per_hour >= chat_per_minute AND chat_global_per_minute >= 1 AND tool_per_minute >= 1", name="ck_usage_limits_requests"))
    op.execute("INSERT INTO usage_limit_settings (id) VALUES (1)")
    op.create_table("rate_limit_buckets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("used >= 1", name="ck_rate_limit_used"))
    op.create_index("ix_rate_limit_buckets_reset_at", "rate_limit_buckets", ["reset_at"])


def downgrade():
    op.drop_table("rate_limit_buckets")
    op.drop_table("usage_limit_settings")
