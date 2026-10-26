"""Delete personal chat content while retaining minimal replay markers.

Revision ID: e8c31d52a904
Revises: d4e91b70a6f2
"""
from alembic import op
import sqlalchemy as sa

revision = "e8c31d52a904"
down_revision = "d4e91b70a6f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_conversations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    # Never make a deleted conversation visible again on downgrade.
    op.execute("DELETE FROM chat_turns WHERE conversation_id IN (SELECT id FROM chat_conversations WHERE deleted_at IS NOT NULL)")
    op.execute("DELETE FROM chat_conversations WHERE deleted_at IS NOT NULL")
    op.drop_column("chat_conversations", "deleted_at")
