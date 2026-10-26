"""Add member tool grants, denied by default."""

from alembic import op
import sqlalchemy as sa


revision = "61d5e40b8a72"
down_revision = "4c9b6f10a2d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tool_grants",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("granted_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "server_id", "tool_name"),
        sa.CheckConstraint("length(tool_name) > 0", name="ck_tool_grants_name"),
    )
    op.create_index("ix_tool_grants_server_id", "tool_grants", ["server_id"])


def downgrade():
    op.drop_index("ix_tool_grants_server_id", table_name="tool_grants")
    op.drop_table("tool_grants")
