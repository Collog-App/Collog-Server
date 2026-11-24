"""통화마다 외부 AI 공급자가 응답으로 알려 준 사용량(calls.ai_usage)."""

import sqlalchemy as sa
from alembic import op

revision = "0022_call_ai_usage"
down_revision = "0021_app_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("ai_usage", sa.JSON(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
