"""앱 이용 기록(app_events), 통화별 전화 알림 결과(calls.incoming_push),
운영 화면의 테스트 그룹(ops_account_flags.cohort)."""

import sqlalchemy as sa
from alembic import op

revision = "0021_app_events"
down_revision = "0020_call_summary_and_timing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("code", sa.String(40), nullable=True),
        sa.Column("subject_id", sa.String(36), nullable=True),
        sa.Column("call_id", sa.String(36), nullable=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("app_version", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_app_events_user_id", "app_events", ["user_id"])
    op.create_index("ix_app_events_occurred_at", "app_events", ["occurred_at"])
    op.create_index("ix_app_events_received_at", "app_events", ["received_at"])
    op.add_column("calls", sa.Column("incoming_push", sa.JSON(), nullable=True))
    op.add_column("ops_account_flags", sa.Column("cohort", sa.String(16), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
