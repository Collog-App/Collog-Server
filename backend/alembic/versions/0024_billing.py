"""구독 결제: 분석 분량 원장(analysis_usage), RevenueCat 구독(user_entitlements),
처리한 웹훅(revenuecat_events), 계정별 결제 미리 켜기(ops_account_flags.billing_preview).

0023 은 다른 브랜치가 예약해 두었다. 그 브랜치가 먼저 들어가면 down_revision 을 0023 으로 바꾼다.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_billing"
down_revision = "0022_call_ai_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_usage",
        sa.Column("call_id", sa.String(36), primary_key=True),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("child_id", sa.String(36), nullable=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("parent_id", "child_id", "month"):
        op.create_index(f"ix_analysis_usage_{column}", "analysis_usage", [column])
    op.create_table(
        "user_entitlements",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("store", sa.String(32), nullable=True),
        sa.Column("product_id", sa.String(120), nullable=True),
        sa.Column("environment", sa.String(16), nullable=True),
        sa.Column("will_renew", sa.Boolean(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "revenuecat_events",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("app_user_id", sa.String(100), nullable=True),
        sa.Column("environment", sa.String(16), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revenuecat_events_app_user_id", "revenuecat_events", ["app_user_id"])
    op.add_column(
        "ops_account_flags",
        sa.Column("billing_preview", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
