"""Persist registry bootstrap state."""

from alembic import op
import sqlalchemy as sa


revision = "4c9b6f10a2d1"
down_revision = "8bd39ee59e3f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registry_bootstrap",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=False,
            nullable=False,
        ),
        sa.CheckConstraint(
            "id = 1",
            name="ck_registry_bootstrap_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Existing installations have already performed their initial setup.
    # A fresh database remains unmarked until startup seeds its defaults.
    op.execute(sa.text("""
        INSERT INTO registry_bootstrap (id)
        SELECT 1
        WHERE EXISTS (SELECT 1 FROM mcp_servers)
           OR EXISTS (SELECT 1 FROM users)
    """))


def downgrade():
    op.drop_table("registry_bootstrap")
