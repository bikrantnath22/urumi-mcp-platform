"""Track the analytics MCP registration for each managed store."""

from alembic import op
import sqlalchemy as sa


revision = "6a18dc43f2b7"
down_revision = "4d2a8f19c6b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("analytics_mcp_server_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_stores_analytics_mcp_server_id",
        "stores",
        "mcp_servers",
        ["analytics_mcp_server_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_stores_analytics_mcp_server_id",
        "stores",
        ["analytics_mcp_server_id"],
    )
    op.create_check_constraint(
        "ck_stores_distinct_mcp_servers",
        "stores",
        "mcp_server_id IS NULL OR analytics_mcp_server_id IS NULL "
        "OR mcp_server_id <> analytics_mcp_server_id",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_stores_distinct_mcp_servers", "stores", type_="check",
    )
    op.drop_constraint(
        "uq_stores_analytics_mcp_server_id", "stores", type_="unique",
    )
    op.drop_constraint(
        "fk_stores_analytics_mcp_server_id", "stores", type_="foreignkey",
    )
    op.drop_column("stores", "analytics_mcp_server_id")
