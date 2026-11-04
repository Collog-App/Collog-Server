import sqlalchemy as sa
from alembic import op

revision = "0016_two_participant_analysis"
down_revision = "0015_kakao_login"
branch_labels = None
depends_on = None

SUBJECT_TABLES = (
    "health_extractions",
    "extraction_evidence",
    "acoustic_analysis_runs",
    "acoustic_features",
    "repeat_events",
)
# 지금까지 통화당 한 행이던 표. 두 사람을 분석하면 통화 × 대상마다 한 행이 된다.
PER_CALL_TABLES = ("health_extractions", "extraction_evidence", "acoustic_analysis_runs")


def upgrade() -> None:
    with op.batch_alter_table("calls") as batch:
        batch.add_column(
            sa.Column("analysis_mode", sa.String(16), server_default="SINGLE", nullable=False)
        )
        batch.add_column(sa.Column("question_target_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_calls_question_target", "users", ["question_target_id"], ["id"]
        )
        batch.create_index("ix_calls_question_target_id", ["question_target_id"])
    op.execute(sa.text("UPDATE calls SET question_target_id = parent_id"))

    op.create_table(
        "call_participants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("call_id", sa.String(36), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("speaker_label", sa.String(16), nullable=False),
        sa.Column("analysis_status", sa.String(16), nullable=False),
        sa.Column("exclusion_reason", sa.String(48), nullable=True),
        sa.Column("speech_sec", sa.Integer(), nullable=True),
        sa.Column("stt_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_call_participants_user_id", "call_participants", ["user_id"])
    op.create_index(
        "uq_call_participants_call_user", "call_participants", ["call_id", "user_id"], unique=True
    )

    with op.batch_alter_table("audio_assets") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_audio_assets_owner", "users", ["owner_user_id"], ["id"])
    op.execute(sa.text(
        "UPDATE audio_assets SET owner_user_id = ("
        " SELECT CASE WHEN audio_assets.kind = 'WEBRTC_EGRESS_CHILD'"
        " THEN calls.child_id ELSE calls.parent_id END"
        " FROM calls WHERE calls.id = audio_assets.call_id)"
    ))

    for table in SUBJECT_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("subject_user_id", sa.String(36), nullable=True))
            batch.create_foreign_key(f"fk_{table}_subject", "users", ["subject_user_id"], ["id"])
            batch.create_index(f"ix_{table}_subject_user_id", ["subject_user_id"])
        op.execute(sa.text(
            f"UPDATE {table} SET subject_user_id ="
            f" (SELECT calls.parent_id FROM calls WHERE calls.id = {table}.call_id)"
        ))
    for table in PER_CALL_TABLES:
        op.drop_index(f"ix_{table}_call_id", table_name=table)
        op.create_index(f"ix_{table}_call_id", table, ["call_id"])
        op.create_index(
            f"uq_{table}_call_subject", table, ["call_id", "subject_user_id"], unique=True
        )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
