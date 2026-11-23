from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ops import Dataset, account_label, build_summary
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family

CODE = "test-ops-code"
NOW = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)  # 한국 시간 12:00


def make_client(tmp_path: Path, codes: str) -> TestClient:
    settings = Settings(
        app_env="test",
        schema_auto_reset=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ops.db'}",
        local_storage_path=tmp_path / "audio",
        public_base_url="http://testserver",
        jwt_secret="test-secret-with-more-than-thirty-two-characters",
        mock_external_services=True,
        raw_audio_wait_seconds=0,
        storage_backend="local",
        allow_raw_only_analysis=False,
        question_tts_provider="ios_local",
        elevenlabs_api_key="",
        elevenlabs_voice_id="",
        ops_access_codes=codes,
    )
    return TestClient(create_app(settings))


@pytest.fixture
def ops_client(tmp_path: Path) -> Iterator[TestClient]:
    digest = hashlib.sha256(CODE.encode()).hexdigest()
    with make_client(tmp_path, f"tester:{digest}") as client:
        yield client


def test_ops_is_hidden_without_configured_codes(tmp_path: Path) -> None:
    with make_client(tmp_path, "") as client:
        assert client.get("/ops").status_code == 404
        assert client.get("/ops/api/summary", headers={"X-Ops-Code": CODE}).status_code == 404


def test_code_entries_are_normalized_and_bad_ones_skipped() -> None:
    digest = hashlib.sha256(CODE.encode()).hexdigest()
    settings = Settings(ops_access_codes=f"bad:not-a-hash, upper:{digest.upper()} ,:{digest}")
    assert settings.ops_access_code_hashes == [("upper", digest)]
    assert Settings(ops_access_codes="bad:not-a-hash").ops_access_code_hashes == []


def test_ops_page_loads_but_data_needs_the_code(ops_client: TestClient) -> None:
    page = ops_client.get("/ops")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert ops_client.get("/ops/app.js").status_code == 200
    assert ops_client.get("/ops/api/summary").status_code == 401
    assert ops_client.get("/ops/api/summary", headers={"X-Ops-Code": "wrong"}).status_code == 401
    assert ops_client.get("/ops/api/accounts").status_code == 401


def test_marking_an_account_internal_removes_its_calls(ops_client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(ops_client)
    created = ops_client.post(
        "/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]}
    ).json()
    accepted = ops_client.post(f"/v1/calls/{created['callId']}/accept", headers=auth(parent_token))
    assert accepted.status_code == 200, accepted.text
    headers = {"X-Ops-Code": CODE}

    summary = ops_client.get("/ops/api/summary", headers=headers).json()
    assert summary["headline"]["connectedCalls"] == 1
    assert summary["headline"]["linkedFamilies"]["n"] == 1

    accounts = ops_client.get("/ops/api/accounts", headers=headers).json()["accounts"]
    assert {a["id"] for a in accounts} == {child["id"], parent["id"]}
    assert all("phone" not in a and "email" not in a and "name" not in a for a in accounts)
    assert {a["label"] for a in accounts} == {"김철수", "김순자"}

    marked = ops_client.put(
        f"/ops/api/accounts/{child['id']}/internal", headers=headers, json={"internal": True}
    )
    assert marked.status_code == 200
    summary = ops_client.get("/ops/api/summary", headers=headers).json()
    assert summary["headline"]["connectedCalls"] == 0
    assert summary["headline"]["linkedFamilies"]["n"] == 0
    assert summary["scope"]["internalAccounts"] == 1

    included = ops_client.get("/ops/api/summary?internal=true", headers=headers).json()
    assert included["headline"]["connectedCalls"] == 1

    missing = ops_client.put(
        "/ops/api/accounts/nobody/internal", headers=headers, json={"internal": True}
    )
    assert missing.status_code == 404


def test_deleting_an_account_removes_its_internal_mark(
    ops_client: TestClient, tmp_path: Path
) -> None:
    token, person = create_user(ops_client, "01055556666", "CHILD", "시험")
    headers = {"X-Ops-Code": CODE}
    marked = ops_client.put(
        f"/ops/api/accounts/{person['id']}/internal", headers=headers, json={"internal": True}
    )
    assert marked.status_code == 200
    assert (
        ops_client.get("/ops/api/summary", headers=headers).json()["scope"]["internalAccounts"] == 1
    )

    assert ops_client.delete("/v1/account", headers=auth(token)).status_code == 204
    with sqlite3.connect(tmp_path / "ops.db") as connection:
        rows = connection.execute("select count(*) from ops_account_flags").fetchone()[0]
    assert rows == 0


# ------------------------------------------------------------ 계산 규칙


def user(uid: str, role: str, created: datetime, nickname: str | None = "닉") -> SimpleNamespace:
    return SimpleNamespace(
        id=uid,
        role=role,
        nickname=nickname,
        name="이름",
        gender="FEMALE",
        birth_date=date(1960, 5, 1),
        marketing_opt_in=False,
        created_at=created,
        apple=False,
        google=True,
        kakao=False,
        phone=False,
    )


def call(parent: str, child: str, accepted: datetime | None, state: str = "ANALYZED"):
    return SimpleNamespace(
        parent_id=parent,
        child_id=child,
        caller_id=child,
        state=state,
        started_at=accepted or NOW,
        accepted_at=accepted,
        duration_sec=300 if accepted else None,
        parent_speech_sec=120 if accepted else None,
        recording_enabled=True,
        report_notified_at=None,
        ended_at=None,
        analysis_timing=None,
    )


def dataset() -> Dataset:
    data = Dataset()
    start = NOW - timedelta(days=40)
    for uid, role in (("c1", "CHILD"), ("p1", "PARENT"), ("c2", "CHILD"), ("p2", "PARENT")):
        data.users[uid] = user(uid, role, start)
        data.touch(uid, start)
    data.users["late"] = user("late", "CHILD", NOW - timedelta(days=1), nickname=None)
    data.family_users = [(start, {"c1", "p1"}), (start, {"c2", "p2"}), (start, {"late"})]
    data.calls = [
        # 1쌍: 첫 통화 뒤 3일·10일에 다시 → 14일 안 재통화, 같은 날 두 번은 하루로
        call("p1", "c1", start),
        call("p1", "c1", start + timedelta(minutes=5)),
        call("p1", "c1", start + timedelta(days=3)),
        call("p1", "c1", NOW - timedelta(days=2)),
        call("p1", "c1", NOW - timedelta(days=9)),
        # 2쌍: 한 번만 받음 + 안 받은 전화
        call("p2", "c2", start),
        call("p2", "c2", None, state="ANALYSIS_EXCLUDED"),
    ]
    for c in data.calls:
        if c.accepted_at:
            data.touch(c.parent_id, c.accepted_at)
            data.touch(c.child_id, c.accepted_at)
    return data


def test_summary_counts_pairs_repeat_and_frequency() -> None:
    summary = build_summary(dataset(), now=NOW, include_internal=False, since=None)
    h = summary["headline"]
    assert h["signedUp"] == 4
    assert h["loginOnly"] == 1
    assert h["linkedFamilies"] == {"n": 2, "d": 3, "pct": 66.7}
    assert h["callingPairs"] == 2
    assert h["connectedCalls"] == 6
    assert h["repeatWithin14d"] == {"n": 1, "d": 2, "pct": 50.0}
    # 최근 28일: 1쌍만 2일 통화 → 주당 0.5일
    assert h["callDaysPerWeek"]["pairs"] == 1
    assert h["callDaysPerWeek"]["median"] == 0.5
    assert h["active7d"] == {"n": 2, "d": 4, "pct": 50.0}
    assert h["returnedAfter7d"] == {"n": 2, "d": 4, "pct": 50.0}
    funnel = summary["calls"]["all"]
    assert funnel["attempts"] == 7
    assert funnel["connected"] == {"n": 6, "d": 7, "pct": 85.7}


def test_internal_accounts_and_since_filter_remove_people() -> None:
    data = dataset()
    data.internal = {"c1"}
    summary = build_summary(data, now=NOW, include_internal=False, since=None)
    assert summary["headline"]["callingPairs"] == 1
    assert summary["headline"]["signedUp"] == 3

    recent = build_summary(data, now=NOW, include_internal=True, since=date(2026, 11, 20))
    assert recent["headline"]["signedUp"] == 0
    assert recent["headline"]["loginOnly"] == 1
    assert recent["headline"]["connectedCalls"] == 0


def test_weekly_trend_has_twelve_monday_weeks() -> None:
    weekly = build_summary(dataset(), now=NOW, include_internal=False, since=None)["weekly"]
    assert len(weekly) == 12
    assert all(date.fromisoformat(w["week"]).weekday() == 0 for w in weekly)
    assert weekly[-1]["week"] == "2026-11-16"
    assert sum(w["connectedCalls"] for w in weekly) == 6


def test_ios_accounts_without_nickname_count_as_signed_up() -> None:
    data = dataset()
    ios = user("ios", "PARENT", NOW - timedelta(days=3), nickname=None)
    ios.google, ios.apple = False, True
    data.users["ios"] = ios
    h = build_summary(data, now=NOW, include_internal=False, since=None)["headline"]
    assert h["signedUp"] == 5
    assert h["loginOnly"] == 1  # 안드로이드(Google) 가입 단계에서 멈춘 late 만


def test_account_label_shows_full_name_with_nickname() -> None:
    assert account_label(None, "홍길동") == "홍길동"
    assert account_label("Emul1", "Hong Gildong") == "Emul1 (Hong Gildong)"
    assert account_label("홍길동", "홍길동") == "홍길동"
    assert account_label("응애", None) == "응애"
    assert account_label(None, " ") == "(이름 없음)"


def test_analysis_time_uses_only_analyzed_calls_that_recorded_it() -> None:
    data = Dataset()
    start = NOW - timedelta(days=3)
    data.users = {"p": user("p", "PARENT", start), "c": user("c", "CHILD", start)}
    data.family_users = [(start, {"p", "c"})]
    timed = [
        {"totalSec": 40.0, "waitSec": 20.0, "sttSec": 8.0, "analysisSec": 10.0},
        {"totalSec": 60.0, "waitSec": 30.0, "sttSec": 12.0, "analysisSec": 14.0},
        {"totalSec": 90.0, "waitSec": 50.0, "sttSec": 20.0, "analysisSec": 16.0},
    ]
    for index, timing in enumerate(timed):
        item = call("p", "c", NOW - timedelta(hours=index + 1))
        item.analysis_timing = timing
        item.ended_at = item.accepted_at + timedelta(minutes=5)
        item.report_notified_at = item.ended_at + timedelta(seconds=timing["totalSec"] + 5)
        data.calls.append(item)
    old = call("p", "c", NOW - timedelta(hours=9))  # 기록이 생기기 전 통화
    excluded = call("p", "c", NOW - timedelta(hours=10), state="ANALYSIS_EXCLUDED")
    excluded.analysis_timing = {"totalSec": 999.0}
    data.calls += [old, excluded]

    time = build_summary(data, now=NOW, include_internal=False, since=None)["calls"]["analysisTime"]
    assert time == {
        "count": 3,
        "totalSec": 60,
        "waitSec": 30,
        "sttSec": 12,
        "analysisSec": 14,
        "notifyCount": 3,
        "notifySec": 65,
    }

    empty = build_summary(Dataset(), now=NOW, include_internal=False, since=None)
    assert empty["calls"]["analysisTime"]["totalSec"] is None
    assert empty["calls"]["analysisTime"]["notifySec"] is None
