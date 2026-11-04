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


def test_two_participant_migration_backfills_the_single_subject(tmp_path: Path) -> None:
    database = tmp_path / "s3.db"
    result = migrate(database, "upgrade", "0015_kakao_login")
    assert result.returncode == 0, result.stderr
    engine = sa.create_engine(f"sqlite:///{database}")
    now = datetime.now(UTC)
    rows = {
        "users": [
            dict(id="parent", role="PARENT", name="부모", created_at=now),
            dict(id="child", role="CHILD", name="자녀", created_at=now),
        ],
        "calls": [dict(
            id="call", parent_id="parent", child_id="child", caller_id="child", state="ANALYZED",
            room_name="room", recording_enabled=True, started_at=now, asked_question_ids="[]",
            created_at=now,
        )],
        "audio_assets": [
            dict(id=kind, call_id="call", kind=kind, uri=f"local://{kind}",
                 content_type="audio/ogg", status="PURGED", created_at=now)
            for kind in ("DEVICE_RAW", "WEBRTC_EGRESS_PARENT", "WEBRTC_EGRESS_CHILD")
        ],
        "health_extractions": [dict(id="x", call_id="call", parse_status="OK", created_at=now)],
        "extraction_evidence": [
            dict(id="e", call_id="call", facts="[]", schema_version="v2", created_at=now)
        ],
        "acoustic_analysis_runs": [dict(
            id="r", call_id="call", analyzer_version="v", cough_detector_version="v",
            created_at=now,
        )],
        "acoustic_features": [dict(
            id="f", call_id="call", audio_source="DEVICE_RAW", metric="SPEECH_RATE",
            value=1.0, unit="u", status="OK", observed_at=now,
        )],
        "repeat_events": [dict(
            id="p", call_id="call", speaker="PARENT", start_ms=0, end_ms=1,
            category="REPEAT_REQUEST", matched_text="뭐라고", rule_id="r", confidence=1.0,
            rule_version="v", created_at=now,
        )],
    }
    with engine.begin() as connection:
        metadata = sa.MetaData()
        metadata.reflect(connection)
        for table, values in rows.items():
            connection.execute(metadata.tables[table].insert(), values)
    result = migrate(database, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        call = connection.execute(
            sa.text("SELECT analysis_mode, question_target_id FROM calls")
        ).one()
        assert tuple(call) == ("SINGLE", "parent")
        owners = dict(
            connection.execute(sa.text("SELECT kind, owner_user_id FROM audio_assets")).all()
        )
        assert owners == {
            "DEVICE_RAW": "parent",
            "WEBRTC_EGRESS_PARENT": "parent",
            "WEBRTC_EGRESS_CHILD": "child",
        }
        for table in rows:
            if table in {"users", "calls", "audio_assets"}:
                continue
            subject = connection.scalar(sa.text(f"SELECT subject_user_id FROM {table}"))
            assert subject == "parent", table
        assert connection.scalar(sa.text("SELECT count(*) FROM call_participants")) == 0
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    engine.dispose()


def test_destructive_downgrade_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "rollback.db"
    assert migrate(database, "upgrade", "head").returncode == 0
    result = migrate(database, "downgrade", "base")
    assert result.returncode != 0
    assert "Restore a verified backup" in result.stderr
