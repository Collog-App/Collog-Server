"""PAUSE_RATIO 정의 변경(collog-acoustic-v6) 전에 저장된 값을 기준선에서 뺀다.

v5까지의 PAUSE_RATIO는 Deepgram segment 안의 간격만 세서 구조적으로 0이었다. 이 값이 남아
있으면 첫 4주로 고정되는 ANCHOR 기준선이 0으로 굳어 v6 값과 비교할 수 없다. 기준선·리포트·
변화 신호는 status OK만 읽으므로 옛 값을 UNMEASURABLE로 바꾸면 모두에서 빠진다.
"""

from alembic import op

revision = "0018_pause_ratio_v6"
down_revision = "0017_todo_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE acoustic_features
        SET status = 'UNMEASURABLE', value = NULL, unmeasurable_reason = 'DEFINITION_CHANGED'
        WHERE metric = 'PAUSE_RATIO'
          AND status = 'OK'
          AND call_id NOT IN (
            SELECT call_id FROM acoustic_analysis_runs
            WHERE analyzer_version = 'collog-acoustic-v6'
          )
        """
    )
    # 다음 통화 때 다시 만들어지지만, 그 전에 리포트가 옛 기준선을 보여주지 않도록 지운다.
    op.execute("DELETE FROM baselines WHERE metric = 'PAUSE_RATIO'")


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of a destructive downgrade")
