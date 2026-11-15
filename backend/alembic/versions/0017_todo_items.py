import sqlalchemy as sa
from alembic import op

revision = "0017_todo_items"
down_revision = "0016_health_daily_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "todo_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("suggestion_key", sa.String(40), nullable=True),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_todo_items_user_id", "todo_items", ["user_id"])


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
