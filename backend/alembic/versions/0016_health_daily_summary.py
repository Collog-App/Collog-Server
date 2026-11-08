import sqlalchemy as sa
from alembic import op

revision = "0016_health_daily_summary"
down_revision = "0015_kakao_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_link_consents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("document_version", sa.String(40), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("agreed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
    )
    op.create_index("ix_health_link_consents_user_id", "health_link_consents", ["user_id"])
    op.create_table(
        "health_daily_summaries",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("sleep_minutes", sa.Integer(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("resting_heart_rate", sa.Integer(), nullable=True),
        sa.Column("blood_pressure_systolic", sa.Integer(), nullable=True),
        sa.Column("blood_pressure_diastolic", sa.Integer(), nullable=True),
        sa.Column("blood_glucose_mg_dl", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("health_extractions") as batch:
        batch.add_column(sa.Column("safety_notice", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
