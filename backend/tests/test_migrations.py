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


def test_destructive_downgrade_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "rollback.db"
    assert migrate(database, "upgrade", "head").returncode == 0
    result = migrate(database, "downgrade", "base")
    assert result.returncode != 0
    assert "Restore a verified backup" in result.stderr
