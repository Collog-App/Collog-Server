import sqlalchemy as sa
from alembic import op

revision = "0009_device_apns_environment"
down_revision = "0008_question_tts_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("apns_environment", sa.String(16), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
