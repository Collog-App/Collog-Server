import sqlalchemy as sa
from alembic import op

revision = "0002_sessions_and_claims"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calls", sa.Column("processing_claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("calls", sa.Column("room_closed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
