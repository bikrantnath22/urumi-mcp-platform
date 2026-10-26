"""Add admin-managed store provisioning settings."""

from alembic import op
import sqlalchemy as sa


revision = "576f196bae1f"
down_revision = "bd6709d1e89d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "store_provisioning_settings",
        sa.Column(
            "id", sa.Integer(),
            autoincrement=False, nullable=False,
        ),
        sa.Column(
            "revision", sa.Integer(),
            server_default=sa.text("1"), nullable=False,
        ),
        sa.Column(
            "enabled", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
        sa.Column(
            "max_per_user", sa.Integer(),
            server_default=sa.text("2"), nullable=False,
        ),
        sa.Column(
            "max_total", sa.Integer(),
            server_default=sa.text("2"), nullable=False,
        ),
        sa.Column(
            "timeout_seconds", sa.Integer(),
            server_default=sa.text("1200"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_by_id", sa.Integer(), nullable=True,
        ),
        sa.CheckConstraint(
            "id = 1",
            name="ck_store_settings_singleton",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_store_settings_revision",
        ),
        sa.CheckConstraint(
            "max_per_user >= 1 "
            "AND max_per_user <= max_total "
            "AND max_total <= 100",
            name="ck_store_settings_limits",
        ),
        sa.CheckConstraint(
            "timeout_seconds >= 60 "
            "AND timeout_seconds <= 3600 "
            "AND timeout_seconds % 60 = 0",
            name="ck_store_settings_timeout",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        "INSERT INTO store_provisioning_settings (id) VALUES (1)"
    )


def downgrade() -> None:
    op.drop_table("store_provisioning_settings")