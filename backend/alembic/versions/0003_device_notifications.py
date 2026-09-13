import sqlalchemy as sa
from alembic import op

revision = "0003_device_notifications"
down_revision = "0002_sessions_and_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "call_notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
