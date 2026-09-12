import sqlalchemy as sa
from alembic import op

revision = "0004_report_notifications"
down_revision = "0003_device_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("push_token", sa.Text(), nullable=True))
    op.add_column(
        "devices",
        sa.Column(
            "report_notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "calls", sa.Column("report_notified_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
