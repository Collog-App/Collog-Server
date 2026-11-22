"""운영 지표 화면(/ops)에서 팀·시험 계정을 빼기 위한 표시."""

import sqlalchemy as sa
from alembic import op

revision = "0019_ops_account_flags"
down_revision = "0018_pause_ratio_v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_account_flags",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("internal", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
