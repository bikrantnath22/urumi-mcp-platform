"""One-use WordPress password setup capabilities; passwords stay in WordPress."""
from alembic import op
import sqlalchemy as sa

revision = "e53cb92a071d"
down_revision = "d18fa734c902"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "store_wordpress_setup",
        sa.Column("store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("access_revision", sa.Integer(), nullable=False),
        sa.Column("namespace_uid", sa.String(256), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("generation >= 1 AND access_revision >= 1 AND owner_id >= 1", name="ck_wp_setup_versions"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_wp_setup_hash"),
    )


def downgrade():
    op.drop_table("store_wordpress_setup")
