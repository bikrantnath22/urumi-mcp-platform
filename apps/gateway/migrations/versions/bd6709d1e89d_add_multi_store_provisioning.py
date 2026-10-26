"""add multi-store provisioning

Revision ID: bd6709d1e89d
Revises: e8c31d52a904
Create Date: 2026-10-16 06:46:05.665815

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bd6709d1e89d'
down_revision: Union[str, Sequence[str], None] = 'e8c31d52a904'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    op.create_table('stores',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('slug', sa.String(length=32), nullable=False),
    sa.Column('namespace', sa.String(length=63), nullable=False),
    sa.Column('release_name', sa.String(length=53), server_default=sa.text("'store'"), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default=sa.text("'Provisioning'"), nullable=False),
    sa.Column('desired_state', sa.String(length=8), server_default=sa.text("'present'"), nullable=False),
    sa.Column('generation', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('status_reason', sa.Text(), nullable=True),
    sa.Column('mcp_server_id', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("desired_state IN ('present', 'absent')", name='ck_stores_desired_state'),
    sa.CheckConstraint("status IN ('Provisioning', 'Ready', 'Failed', 'Deleting', 'Deleted')", name='ck_stores_status'),
    sa.CheckConstraint('generation >= 1', name='ck_stores_generation'),
    sa.ForeignKeyConstraint(['mcp_server_id'], ['mcp_servers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mcp_server_id'),
    sa.UniqueConstraint('namespace'),
    sa.UniqueConstraint('slug'),
    sa.UniqueConstraint('url')
    )
    op.create_index('ix_stores_owner_status', 'stores', ['owner_id', 'status'], unique=False)
    op.create_table('store_operations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('store_id', sa.Uuid(), nullable=False),
    sa.Column('requested_by_id', sa.Integer(), nullable=False),
    sa.Column('request_key', sa.Uuid(), nullable=False),
    sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('store_generation', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default=sa.text("'Pending'"), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lease_token', sa.Uuid(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'Running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'Running' AND lease_token IS NULL AND lease_expires_at IS NULL)", name='ck_store_operations_lease'),
    sa.CheckConstraint("action IN ('provision', 'delete')", name='ck_store_operations_action'),
    sa.CheckConstraint("status IN ('Pending', 'Running', 'Succeeded', 'Failed', 'Cancelled')", name='ck_store_operations_status'),
    sa.CheckConstraint('attempts >= 0 AND store_generation >= 1', name='ck_store_operations_counters'),
    sa.CheckConstraint('length(request_fingerprint) = 64', name='ck_store_operations_fingerprint'),
    sa.ForeignKeyConstraint(['requested_by_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_store_operations_lease', 'store_operations', ['status', 'lease_expires_at'], unique=False)
    op.create_index('ix_store_operations_queue', 'store_operations', ['status', 'available_at', 'created_at'], unique=False)
    op.create_index('uq_store_operations_active', 'store_operations', ['store_id'], unique=True, postgresql_where=sa.text("status IN ('Pending', 'Running')"))
    op.create_index('uq_store_operations_request', 'store_operations', ['requested_by_id', 'request_key'], unique=True)
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('uq_store_operations_request', table_name='store_operations')
    op.drop_index('uq_store_operations_active', table_name='store_operations', postgresql_where=sa.text("status IN ('Pending', 'Running')"))
    op.drop_index('ix_store_operations_queue', table_name='store_operations')
    op.drop_index('ix_store_operations_lease', table_name='store_operations')
    op.drop_table('store_operations')
    op.drop_index('ix_stores_owner_status', table_name='stores')
    op.drop_table('stores')
    # ### end Alembic commands ###
