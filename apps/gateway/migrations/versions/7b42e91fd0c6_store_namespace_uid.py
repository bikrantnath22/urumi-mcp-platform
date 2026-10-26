"""Remember the Kubernetes namespace UID for each managed store."""

from alembic import op
import sqlalchemy as sa


revision = "7b42e91fd0c6"
down_revision = "6a18dc43f2b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("namespace_uid", sa.String(length=256), nullable=True),
    )
    op.create_unique_constraint(
        "uq_stores_namespace_uid", "stores", ["namespace_uid"],
    )
    op.create_check_constraint(
        "ck_stores_namespace_uid_length",
        "stores",
        "namespace_uid IS NULL OR length(namespace_uid) BETWEEN 1 AND 256",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_stores_namespace_uid_length", "stores", type_="check",
    )
    op.drop_constraint(
        "uq_stores_namespace_uid", "stores", type_="unique",
    )
    op.drop_column("stores", "namespace_uid")
