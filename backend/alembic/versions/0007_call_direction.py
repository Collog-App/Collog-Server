import sqlalchemy as sa
from alembic import op

revision = "0007_call_direction"
down_revision = "0006_apple_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("calls") as batch:
        batch.add_column(sa.Column("caller_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_calls_caller", "users", ["caller_id"], ["id"])
        batch.create_index("ix_calls_caller_id", ["caller_id"])
    op.execute(sa.text("UPDATE calls SET caller_id = child_id WHERE caller_id IS NULL"))


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
