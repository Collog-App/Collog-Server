from __future__ import annotations

import asyncio
import hashlib
import re
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app import ops
from app.config import Settings
from app.database import Base, Database
from app.models import CallRecord, CallState
from app.services.livekit import (
    LiveKitError,
    LiveKitGateway,
    MockLiveKitGateway,
    RealLiveKitGateway,
)
from tests.test_api_flow import onboard_family
from tests.test_ops_dashboard import make_client

BACKEND = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 3, 0, tzinfo=UTC)


def digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def test_monitor_code_opens_only_alerts(tmp_path: Path) -> None:
    codes = f"tester:{digest('operator-code')},monitor:{digest('monitor-code')}"
    with make_client(tmp_path, codes) as client:
        _, child, _, _ = onboard_family(client)
        monitor = {"X-Ops-Code": "monitor-code"}
        operator = {"X-Ops-Code": "operator-code"}
        blocked = [
            client.get("/ops/api/summary", headers=monitor),
            client.get("/ops/api/accounts", headers=monitor),
            client.put(f"/ops/api/accounts/{child['id']}/internal", headers=monitor,
                       json={"internal": True}),
            client.put(f"/ops/api/accounts/{child['id']}/cohort", headers=monitor,
                       json={"cohort": "AWARE"}),
            client.put(f"/ops/api/accounts/{child['id']}/billing-preview", headers=monitor,
                       json={"billingPreview": True}),
        ]
        assert [response.status_code for response in blocked] == [403] * 5
        assert all(child["id"] not in response.text for response in blocked)

        for headers in (monitor, operator):
            response = client.get("/ops/api/alerts", headers=headers)
            assert response.status_code == 200
            assert response.json() == {"ok": True, "problems": []}
        assert client.get("/ops/api/alerts").status_code == 401
        assert client.get("/ops/api/alerts", headers={"X-Ops-Code": "wrong"}).status_code == 401
        assert client.get("/ops/api/summary", headers=operator).status_code == 200


def test_default_access_codes_include_the_monitor_label() -> None:
    compose = (BACKEND / "docker-compose.yml").read_text()
    default = re.search(r"OPS_ACCESS_CODES: \$\{OPS_ACCESS_CODES-([^}]*)\}", compose)
    assert default is not None
    entries = Settings(ops_access_codes=default.group(1)).ops_access_code_hashes
    assert [label for label, _ in entries] == ["yongjin", "team", "monitor"]
    assert dict(entries)["monitor"] == (
        "2b3af1564542e940656ce245ee1f0140e4def5d0506c98cbc5323786d14f53cb"
    )


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    created = Database(f"sqlite+aiosqlite:///{tmp_path / 'alerts.db'}")
    async with created.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield created
    finally:
        await created.close()


async def add_calls(database: Database, *calls: tuple[str, bool, timedelta]) -> None:
    async with database.sessions() as session:
        for state, recorded, ended_ago in calls:
            session.add(CallRecord(
                parent_id="parent",
                child_id="child",
                state=state,
                room_name=f"room-{uuid4()}",
                recording_enabled=recorded,
                ended_at=NOW - ended_ago,
            ))
        await session.commit()


async def problems(database: Database, livekit: LiveKitGateway | None = None) -> list[dict]:
    async with database.sessions() as session:
        return await ops.find_problems(session, livekit or MockLiveKitGateway(), NOW)


class SlowLiveKit(MockLiveKitGateway):
    async def check_reachable(self) -> None:
        await asyncio.sleep(1)


class BrokenLiveKit(MockLiveKitGateway):
    async def check_reachable(self) -> None:
        raise LiveKitError("down")


async def test_unreachable_livekit_is_a_problem(database: Database, monkeypatch) -> None:
    monkeypatch.setattr(ops, "LIVEKIT_TIMEOUT_SECONDS", 0.05)
    assert await problems(database) == []
    for livekit in (SlowLiveKit(), BrokenLiveKit()):
        found = await problems(database, livekit)
        assert [p["code"] for p in found] == ["LIVEKIT_UNREACHABLE"]
        assert "LiveKit" in found[0]["message"]


async def test_analysis_backlog_counts_recorded_calls_waiting_too_long(database: Database) -> None:
    minutes = timedelta(minutes=1)
    await add_calls(
        database,
        (CallState.PROCESSING.value, True, 35 * minutes),
        (CallState.ENDED.value, True, 30 * minutes),
        (CallState.ENDED.value, True, 25 * minutes),  # 아직 30분이 안 됨
        (CallState.ENDED.value, False, 60 * minutes),  # 녹음 안 한 통화
        (CallState.ANALYZED.value, True, 60 * minutes),
    )
    found = await problems(database)
    assert [p["code"] for p in found] == ["ANALYSIS_BACKLOG"]
    assert "2건" in found[0]["message"]


@pytest.mark.parametrize(
    "history,expected",
    [
        # (상태, 몇 시간 전에 끝났나) — 위가 최근
        ([("F", 1), ("F", 2), ("F", 3)], True),
        ([("F", 1), ("X", 2), ("F", 3), ("F", 4)], True),  # 분석 제외는 건너뜀
        ([("F", 25), ("F", 26), ("F", 27)], False),  # 마지막이 24시간보다 전
        ([("F", 1), ("F", 2), ("A", 3), ("F", 4)], False),
        ([("F", 1), ("F", 2)], False),
    ],
)
async def test_three_failed_analyses_in_a_row_are_a_problem(
    database: Database, history: list[tuple[str, int]], expected: bool
) -> None:
    states = {
        "F": CallState.ANALYSIS_FAILED.value,
        "A": CallState.ANALYZED.value,
        "X": CallState.ANALYSIS_EXCLUDED.value,
    }
    await add_calls(
        database, *((states[s], True, timedelta(hours=hours)) for s, hours in history)
    )
    found = await problems(database)
    assert [p["code"] for p in found] == (["ANALYSIS_FAILING"] if expected else [])


async def test_real_livekit_check_reports_a_closed_server() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = Settings(
        _env_file=None,
        livekit_url=f"ws://127.0.0.1:{port}",
        livekit_api_key="key",
        livekit_api_secret="secret-with-more-than-thirty-two-characters",
        storage_backend="s3",
    )
    with pytest.raises(LiveKitError):
        await RealLiveKitGateway(settings).check_reachable()


async def test_livekit_client_setup_error_is_an_unreachable_problem(
    database: Database, monkeypatch
) -> None:
    gateway = RealLiveKitGateway(Settings(
        _env_file=None,
        livekit_api_key="key",
        livekit_api_secret="secret-with-more-than-thirty-two-characters",
        storage_backend="s3",
    ))

    def broken_client():
        raise ValueError("bad LiveKit settings")

    monkeypatch.setattr(gateway, "_client", broken_client)
    with pytest.raises(LiveKitError):
        await gateway.check_reachable()
    found = await problems(database, gateway)
    assert [p["code"] for p in found] == ["LIVEKIT_UNREACHABLE"]


def test_backlog_threshold_covers_upload_wait_and_processing_limit() -> None:
    settings = Settings(_env_file=None)
    waits = timedelta(seconds=settings.upload_url_ttl_seconds + settings.processing_timeout_seconds)
    assert ops.ANALYSIS_BACKLOG_AFTER == timedelta(minutes=30)
    assert ops.ANALYSIS_BACKLOG_AFTER > waits
