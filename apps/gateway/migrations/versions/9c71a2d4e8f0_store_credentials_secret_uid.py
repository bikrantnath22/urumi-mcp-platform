"""Track each managed store's credentials Secret identity."""

from alembic import op
import sqlalchemy as sa


revision = "9c71a2d4e8f0"
down_revision = "7b42e91fd0c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("credentials_secret_uid", sa.String(length=256), nullable=True),
    )
    op.create_unique_constraint(
        "uq_stores_credentials_secret_uid", "stores", ["credentials_secret_uid"],
    )
    op.create_check_constraint(
        "ck_stores_credentials_secret_uid_length",
        "stores",
        "credentials_secret_uid IS NULL "
        "OR length(credentials_secret_uid) BETWEEN 1 AND 256",
    )
    op.create_check_constraint(
        "ck_stores_credentials_namespace",
        "stores",
        "credentials_secret_uid IS NULL OR namespace_uid IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_stores_credentials_namespace", "stores", type_="check",
    )
    op.drop_constraint(
        "ck_stores_credentials_secret_uid_length", "stores", type_="check",
    )
    op.drop_constraint(
        "uq_stores_credentials_secret_uid", "stores", type_="unique",
    )
    op.drop_column("stores", "credentials_secret_uid")
