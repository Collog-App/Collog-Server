import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.database import Base

BACKEND = Path(__file__).resolve().parents[1]


def migrate(database: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *arguments],
        cwd=database.parent,
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def baseline() -> sa.MetaData:
    spec = importlib.util.spec_from_file_location("test_baseline", BACKEND / "alembic/baseline.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.baseline_metadata()


def test_empty_database_migrates_to_current_models(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    for _ in range(2):
        result = migrate(database, "upgrade", "head")
        assert result.returncode == 0, result.stderr
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    engine.dispose()


def test_existing_database_is_adopted_without_losing_data(tmp_path: Path) -> None:
    database = tmp_path / "existing.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    metadata = baseline()
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["users"].insert(),
            dict(
                id="existing-user", phone="01000000000", role="PARENT", name="Test",
                created_at=datetime.now(UTC),
            ),
        )
        connection.execute(
            metadata.tables["devices"].insert(),
            dict(
                id="existing-device", user_id="existing-user", platform="ios", token="token",
                created_at=datetime.now(UTC),
            ),
        )
    result = migrate(database, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        saved = connection.scalar(sa.text("SELECT name FROM users WHERE id='existing-user'"))
        assert saved == "Test"
        preferences = connection.execute(
            sa.text("SELECT call_notifications_enabled, report_notifications_enabled FROM devices")
        ).one()
        assert preferences == (True, True)
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    engine.dispose()


@pytest.mark.parametrize("change", ["missing", "type", "unique"])
def test_incompatible_schema_is_refused_without_data_loss(tmp_path: Path, change: str) -> None:
    database = tmp_path / "incompatible.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    metadata = baseline()
    if change == "missing":
        metadata.tables["users"]._columns.remove(metadata.tables["users"].c.name)
    elif change == "type":
        metadata.tables["users"].c.name.type = sa.Integer()
    else:
        metadata.tables["users"].indexes.clear()
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE preserved (value TEXT NOT NULL)"))
        connection.execute(sa.text("INSERT INTO preserved VALUES ('keep')"))
    result = migrate(database, "upgrade", "head")
    assert result.returncode != 0
    assert "Existing schema is incompatible" in result.stderr
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT value FROM preserved")) == "keep"
        assert "processing_claimed_at" not in {
            column["name"] for column in sa.inspect(connection).get_columns("calls")
        }
    engine.dispose()


def test_pause_ratio_saved_before_v6_leaves_the_baseline(tmp_path: Path) -> None:
    # v5까지의 PAUSE_RATIO는 구조적으로 0이었다. ANCHOR 기준선이 0으로 굳지 않도록 빼야 한다.
    database = tmp_path / "pause.db"
    assert migrate(database, "upgrade", "0017_todo_items").returncode == 0
    engine = sa.create_engine(f"sqlite:///{database}")
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for call_id, version in (("old", "collog-acoustic-v5"), ("new", "collog-acoustic-v6")):
            connection.execute(
                sa.text(
                    "INSERT INTO acoustic_analysis_runs VALUES "
                    "(:id, :call_id, :version, 'detector', :now)"
                ),
                dict(id=f"run-{call_id}", call_id=call_id, version=version, now=now),
            )
            for metric, value in (("PAUSE_RATIO", 0.0), ("SPEECH_RATE", 240.0)):
                connection.execute(
                    sa.text(
                        "INSERT INTO acoustic_features (id, call_id, audio_source, metric, value,"
                        " unit, status, observed_at)"
                        " VALUES (:id, :call_id, 'DEVICE_RAW', :metric, :value, '%', 'OK', :now)"
                    ),
                    dict(id=f"{call_id}-{metric}", call_id=call_id, metric=metric, value=value,
                         now=now),
                )
    result = migrate(database, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                sa.text("SELECT id, status FROM acoustic_features ORDER BY id")
            ).all()
        )
    engine.dispose()
    assert rows == {
        "new-PAUSE_RATIO": "OK",
        "new-SPEECH_RATE": "OK",
        "old-PAUSE_RATIO": "UNMEASURABLE",
        "old-SPEECH_RATE": "OK",
    }


def test_app_events_migration_keeps_ops_flags_and_refuses_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    assert migrate(database, "upgrade", "0020_call_summary_and_timing").returncode == 0
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO ops_account_flags VALUES ('tester', 1, :now)"),
            dict(now=datetime.now(UTC)),
        )
    result = migrate(database, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        flag = connection.execute(
            sa.text("SELECT internal, cohort FROM ops_account_flags WHERE user_id='tester'")
        ).one()
        assert tuple(flag) == (1, None)
        inspector = sa.inspect(connection)
        assert "incoming_push" in {column["name"] for column in inspector.get_columns("calls")}
        assert "app_events" in inspector.get_table_names()
    engine.dispose()
    refused = migrate(database, "downgrade", "0020_call_summary_and_timing")
    assert refused.returncode != 0
    assert "Restore a verified backup" in refused.stderr


def test_ai_usage_migration_keeps_existing_calls(tmp_path: Path) -> None:
    database = tmp_path / "usage.db"
    assert migrate(database, "upgrade", "0021_app_events").returncode == 0
    engine = sa.create_engine(f"sqlite:///{database}")
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for user_id in ("p", "c"):
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, role, name, marketing_opt_in, created_at)"
                    " VALUES (:id, 'PARENT', 'Test', 0, :now)"
                ),
                dict(id=user_id, now=now),
            )
        connection.execute(
            sa.text(
                "INSERT INTO calls (id, parent_id, child_id, state, room_name, recording_enabled,"
                " started_at, asked_question_ids, processing_attempts, created_at)"
                " VALUES ('old-call', 'p', 'c', 'ANALYZED', 'room', 1, :now, '[]', 1, :now)"
            ),
            dict(now=now),
        )
    result = migrate(database, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT state, ai_usage FROM calls WHERE id='old-call'")
        ).one()
        assert tuple(row) == ("ANALYZED", None)
    engine.dispose()
    refused = migrate(database, "downgrade", "0021_app_events")
    assert refused.returncode != 0
    assert "Restore a verified backup" in refused.stderr


def test_billing_migration_keeps_users_and_refuses_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "billing.db"
    assert migrate(database, "upgrade", "0022_call_ai_usage").returncode == 0
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (id, role, name, marketing_opt_in, created_at)"
                " VALUES ('u1', 'CHILD', 'Test', 0, :now)"
            ),
            dict(now=datetime.now(UTC)),
        )
    result = migrate(database, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT name FROM users WHERE id='u1'")) == "Test"
        tables = set(sa.inspect(connection).get_table_names())
        assert {"analysis_usage", "user_entitlements", "revenuecat_events"} <= tables
    engine.dispose()
    refused = migrate(database, "downgrade", "0022_call_ai_usage")
    assert refused.returncode != 0
    assert "Restore a verified backup" in refused.stderr


def test_destructive_downgrade_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "rollback.db"
    assert migrate(database, "upgrade", "head").returncode == 0
    result = migrate(database, "downgrade", "base")
    assert result.returncode != 0
    assert "Restore a verified backup" in result.stderr


def test_app_event_indexes_serve_the_limit_count_and_account_deletion(tmp_path: Path) -> None:
    database = tmp_path / "indexes.db"
    assert migrate(database, "upgrade", "head").returncode == 0
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        definitions = dict(connection.execute(sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='app_events'"
        )).all())
        assert "ix_app_events_user_id" not in definitions  # 복합 색인이 대신한다
        assert "(user_id, received_at)" in definitions["ix_app_events_user_id_received_at"]
        for column in ("subject_id", "call_id"):
            assert definitions[f"ix_app_events_{column}"].endswith(f"WHERE {column} IS NOT NULL")

        def plan(statement: str) -> str:
            rows = connection.execute(sa.text(f"EXPLAIN QUERY PLAN {statement}")).all()
            return " ".join(str(row[-1]) for row in rows)

        assert "ix_app_events_user_id_received_at" in plan(
            "SELECT count(*) FROM app_events WHERE user_id = 'u' AND received_at >= '2026-11-24'"
        )
        assert "ix_app_events_subject_id" in plan(
            "UPDATE app_events SET subject_id = NULL WHERE subject_id = 'u'"
        )
        assert "ix_app_events_call_id" in plan(
            "UPDATE app_events SET call_id = NULL WHERE call_id IN ('a', 'b')"
        )
    engine.dispose()
