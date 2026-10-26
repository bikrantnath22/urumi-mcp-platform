"""Store maintenance access, independent of ownership and lifecycle jobs."""
from alembic import op
import sqlalchemy as sa

revision = "d18fa734c902"
down_revision = "c82d6a41f093"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stores", sa.Column("access_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("stores", sa.Column("access_guarded", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("stores", sa.Column("access_revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_stores_access_revision", "stores", "access_revision >= 1")


def downgrade():
    op.drop_constraint("ck_stores_access_revision", "stores", type_="check")
    op.drop_column("stores", "access_revision")
    op.drop_column("stores", "access_guarded")
    op.drop_column("stores", "access_enabled")
