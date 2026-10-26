"""Add admin-managed parallel store creation capacity."""

from alembic import op
import sqlalchemy as sa


revision = "4d2a8f19c6b0"
down_revision = "576f196bae1f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "store_provisioning_settings",
        sa.Column(
            "max_concurrent_creations",
            sa.Integer(),
            server_default=sa.text("2"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_store_settings_concurrency",
        "store_provisioning_settings",
        "max_concurrent_creations >= 1 AND max_concurrent_creations <= 100",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_store_settings_concurrency",
        "store_provisioning_settings",
        type_="check",
    )
    op.drop_column("store_provisioning_settings", "max_concurrent_creations")
