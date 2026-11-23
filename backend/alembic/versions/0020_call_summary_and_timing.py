"""통화 요약(health_extractions.summary)과 분석 단계별 소요 시간(calls.analysis_timing)."""

import sqlalchemy as sa
from alembic import op

revision = "0020_call_summary_and_timing"
down_revision = "0019_ops_account_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("health_extractions", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("calls", sa.Column("analysis_timing", sa.JSON(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
