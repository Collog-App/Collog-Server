import sqlalchemy as sa
from alembic import op

revision = "0008_question_tts_grants"
down_revision = "0007_call_direction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "question_tts_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("call_id", sa.String(36), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("question_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("user_id", "call_id", "created_at"):
        op.create_index(f"ix_question_tts_grants_{name}", "question_tts_grants", [name])


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
