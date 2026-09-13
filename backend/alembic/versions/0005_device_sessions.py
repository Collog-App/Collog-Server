import sqlalchemy as sa
from alembic import op

revision = "0005_device_sessions"
down_revision = "0004_report_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("auth_session_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_devices_auth_session", "refresh_sessions", ["auth_session_id"], ["id"]
        )
        batch.create_index("ix_devices_auth_session_id", ["auth_session_id"])


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
