import sqlalchemy as sa
from alembic import op

revision = "0010_processing_retries"
down_revision = "0009_device_apns_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("accepted_request_id", sa.String(36), nullable=True))
    op.add_column("audio_assets", sa.Column("track_id", sa.String(80), nullable=True))
    op.add_column(
        "calls", sa.Column("processing_attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "calls", sa.Column("processing_retry_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
