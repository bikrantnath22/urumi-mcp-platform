"""Track the PVC and PV identities belonging to each managed store."""

from alembic import op
import sqlalchemy as sa


revision = "c82d6a41f093"
down_revision = "9c71a2d4e8f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The composite foreign key requires this parent constraint first.
    op.create_unique_constraint(
        "uq_stores_id_namespace_uid", "stores", ["id", "namespace_uid"],
    )
    op.create_table(
        "store_volumes",
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("claim_name", sa.String(63), nullable=False),
        sa.Column("namespace_uid", sa.String(256), nullable=False),
        sa.Column("claim_uid", sa.String(256), nullable=False),
        sa.Column("volume_name", sa.String(253), nullable=True),
        sa.Column("volume_uid", sa.String(256), nullable=True),
        sa.Column(
            "storage_class", sa.String(63),
            server_default=sa.text("'local-path'"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["store_id", "namespace_uid"],
            ["stores.id", "stores.namespace_uid"],
            name="fk_store_volumes_namespace", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("store_id", "claim_name"),
        sa.UniqueConstraint("claim_uid", name="uq_store_volumes_claim_uid"),
        sa.UniqueConstraint("volume_name", name="uq_store_volumes_volume_name"),
        sa.UniqueConstraint("volume_uid", name="uq_store_volumes_volume_uid"),
        sa.CheckConstraint(
            "claim_name IN ('data-mysql-0', 'wordpress-data')",
            name="ck_store_volumes_claim_name",
        ),
        sa.CheckConstraint(
            "length(namespace_uid) BETWEEN 1 AND 256 "
            "AND length(claim_uid) BETWEEN 1 AND 256",
            name="ck_store_volumes_claim_uids",
        ),
        sa.CheckConstraint(
            "volume_name IS NULL OR length(volume_name) BETWEEN 1 AND 253",
            name="ck_store_volumes_volume_name",
        ),
        sa.CheckConstraint(
            "volume_uid IS NULL OR length(volume_uid) BETWEEN 1 AND 256",
            name="ck_store_volumes_volume_uid",
        ),
        sa.CheckConstraint(
            "volume_uid IS NULL OR volume_name IS NOT NULL",
            name="ck_store_volumes_volume_pair",
        ),
        sa.CheckConstraint(
            "storage_class = 'local-path'",
            name="ck_store_volumes_storage_class",
        ),
    )


def downgrade() -> None:
    op.drop_table("store_volumes")
    op.drop_constraint("uq_stores_id_namespace_uid", "stores", type_="unique")
