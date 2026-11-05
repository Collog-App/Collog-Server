from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from app.database import Database
from app.models import CallRecord, CallState, User
from app.services.reports import ReportService, aware_datetime, period_bounds


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'snapshot.db'}")
    await database.ensure_schema(auto_reset=True)
    yield database
    await database.close()


async def seed_processing_call(database: Database, ended_at: datetime) -> tuple[str, str]:
    async with database.sessions() as session:
        parent = User(phone="01012345678", name="Parent", role="PARENT")
        child = User(phone="01087654321", name="Child", role="CHILD")
        session.add_all([parent, child])
        await session.flush()
        call = CallRecord(
            parent_id=parent.id,
            child_id=child.id,
            room_name="snapshot-test",
            state=CallState.PROCESSING.value,
            ended_at=ended_at,
        )
        session.add(call)
        await session.commit()
        return call.id, parent.id


async def finish_analysis(database: Database, call_id: str) -> None:
    async with database.sessions() as session:
        await session.execute(
            update(CallRecord)
            .where(CallRecord.id == call_id)
            .values(state=CallState.ANALYZED.value)
        )
        await session.commit()


async def weekly(database: Database, parent_id: str, target) -> dict:
    async with database.sessions() as session:
        return await ReportService().get_or_issue(session, parent_id, "WEEKLY", target)


@pytest.mark.asyncio
async def test_report_issued_while_analysing_picks_up_the_call(database: Database) -> None:
    # 통화가 끝나고 분석이 도는 동안 리포트를 열면 스냅샷이 먼저 저장된다. 분석이 끝난 뒤에는
    # 그 통화의 종료 시각이 스냅샷보다 이르더라도 다시 만들어 새 분석을 넣어야 한다.
    ended = datetime.now(UTC) - timedelta(minutes=5)
    call_id, parent_id = await seed_processing_call(database, ended)
    first = await weekly(database, parent_id, None)
    assert first["analyzedCallCount"] == 0

    await finish_analysis(database, call_id)
    second = await weekly(database, parent_id, None)
    assert second["analyzedCallCount"] == 1

    # 바뀐 것이 없으면 저장된 스냅샷을 그대로 준다.
    third = await weekly(database, parent_id, None)
    assert aware_datetime(third["issuedAt"]) == aware_datetime(second["issuedAt"])


@pytest.mark.asyncio
async def test_past_week_report_picks_up_late_analysis(database: Database) -> None:
    # 일요일 밤 통화의 분석이 월요일에 끝나도 지난주 리포트에 들어가야 한다.
    last_week_start, _ = period_bounds("WEEKLY", (datetime.now(UTC) - timedelta(days=7)).date())
    ended = datetime.combine(last_week_start + timedelta(days=2), datetime.min.time(), tzinfo=UTC)
    call_id, parent_id = await seed_processing_call(database, ended)
    target = last_week_start + timedelta(days=2)
    assert (await weekly(database, parent_id, target))["analyzedCallCount"] == 0

    await finish_analysis(database, call_id)
    assert (await weekly(database, parent_id, target))["analyzedCallCount"] == 1
