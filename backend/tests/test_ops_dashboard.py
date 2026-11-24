from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ops import (
    Dataset,
    account_label,
    build_cohorts,
    build_delivery,
    build_health_link,
    build_quality,
    build_signup,
    build_summary,
)
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_call_summary import LONG_TALK
from tests.test_guardrail import analyze_family_call

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


# ------------------------------------------------------------ 테스트 그룹·전화 알림


def test_operator_sets_a_cohort_that_the_app_never_sees(ops_client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(ops_client)
    headers = {"X-Ops-Code": CODE}
    path = f"/ops/api/accounts/{child['id']}/cohort"

    assert ops_client.put(path, json={"cohort": "AWARE"}).status_code == 401
    saved = ops_client.put(path, headers=headers, json={"cohort": "AWARE"})
    assert saved.status_code == 200
    assert saved.json() == {"id": child["id"], "cohort": "AWARE"}
    accounts = {a["id"]: a for a in ops_client.get("/ops/api/accounts", headers=headers).json()[
        "accounts"
    ]}
    assert accounts[child["id"]]["cohort"] == "AWARE"
    assert accounts[child["id"]]["internal"] is False
    assert accounts[parent["id"]]["cohort"] is None

    summary = ops_client.get("/ops/api/summary", headers=headers).json()
    people = {g["cohort"]: g["people"] for g in summary["cohorts"]["groups"]}
    assert people == {"AWARE": 1, "UNAWARE": 0, None: 1}
    assert len(summary["cohorts"]["weeks"]) == 8

    me = ops_client.patch(
        "/v1/account/preferences", headers=auth(child_token), json={"marketingOptIn": False}
    )
    assert me.status_code == 200
    assert "cohort" not in me.json()

    assert ops_client.put(path, headers=headers, json={"cohort": "BOTH"}).status_code == 422
    cleared = ops_client.put(path, headers=headers, json={"cohort": None})
    assert cleared.json() == {"id": child["id"], "cohort": None}
    missing = ops_client.put(
        "/ops/api/accounts/nobody/cohort", headers=headers, json={"cohort": "UNAWARE"}
    )
    assert missing.status_code == 404


def test_summary_counts_push_results_of_new_calls(ops_client: TestClient) -> None:
    child_token, _, _, parent = onboard_family(ops_client)
    created = ops_client.post(
        "/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]}
    )
    assert created.status_code == 201
    delivery = ops_client.get("/ops/api/summary", headers={"X-Ops-Code": CODE}).json()["delivery"]
    assert delivery["pushResults"] == {"SENT": 0, "TOKEN_INVALID": 0, "FAILED": 0, "NO_DEVICE": 1}
    assert delivery["notRung"] == {"n": 0, "d": 0, "pct": None}


def app_event(user_id: str, name: str, at: datetime, code: str | None = None,
              call_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, name=name, code=code, call_id=call_id, occurred_at=at)


def cohort_dataset() -> Dataset:
    data = Dataset()
    start = NOW - timedelta(days=40)
    for uid, role in (("a1", "CHILD"), ("a2", "CHILD"), ("u1", "CHILD"), ("p1", "PARENT"),
                      ("p2", "PARENT"), ("i1", "CHILD")):
        data.users[uid] = user(uid, role, start)
    data.internal = {"i1"}
    data.cohorts = {"a1": "AWARE", "a2": "AWARE", "i1": "AWARE", "u1": "UNAWARE"}
    data.calls = [
        call("p1", "a1", NOW - timedelta(days=2)),
        call("p1", "a1", NOW - timedelta(days=10)),
        call("p2", "u1", NOW - timedelta(days=20)),
        call("p1", "a1", None),  # 안 받은 전화는 통화가 아니다
        call("p2", "i1", NOW - timedelta(days=1)),
    ]
    data.events = [
        app_event("a1", "report_view", NOW - timedelta(days=2, hours=1), "DIRECT"),  # 통화한 날
        app_event("a2", "report_view", NOW - timedelta(days=1), "DIRECT"),  # 통화 없는 날
        app_event("u1", "report_view", NOW - timedelta(days=20), "NOTIFICATION"),
        app_event("i1", "report_view", NOW - timedelta(days=3), "DIRECT"),
        app_event("a1", "post_call_answer", NOW - timedelta(days=2), "YES"),
        app_event("a1", "post_call_answer", NOW - timedelta(days=10), "NO"),
        app_event("u1", "post_call_answer", NOW - timedelta(days=20), "NOT_SEEN"),
        app_event("a1", "app_open", NOW - timedelta(hours=1)),
        app_event("a2", "app_open", NOW - timedelta(days=8)),
    ]
    return data


def test_cohort_tables_split_people_by_group() -> None:
    cohorts = build_cohorts(cohort_dataset(), now=NOW, include_internal=False, since=None)
    groups = {g["cohort"]: g for g in cohorts["groups"]}
    aware, unaware, undecided = groups["AWARE"], groups["UNAWARE"], groups[None]
    assert aware["people"] == 2  # 팀·시험 계정 i1 은 빠짐
    assert aware["calledLast7d"] == {"n": 1, "d": 2, "pct": 50.0}
    assert aware["reportOnNoCallDay"] == {"n": 1, "d": 2, "pct": 50.0}
    assert aware["questionUsed"] == {"n": 1, "d": 2, "pct": 50.0}
    assert unaware["people"] == 1
    assert unaware["reportOnNoCallDay"] == {"n": 0, "d": 1, "pct": 0.0}
    assert unaware["questionUsed"] == {"n": 0, "d": 1, "pct": 0.0}
    assert undecided["people"] == 2
    assert undecided["calledLast7d"] == {"n": 1, "d": 2, "pct": 50.0}

    assert cohorts["weeks"][-1] == "2026-11-16"
    this_week, last_week = aware["weekly"][-1], aware["weekly"][-2]
    assert this_week == {"week": "2026-11-16", "calls": 1, "callsPerPerson": 0.5, "activeUsers": 1}
    assert last_week == {"week": "2026-11-09", "calls": 1, "callsPerPerson": 0.5, "activeUsers": 1}
    assert sum(w["calls"] for w in undecided["weekly"]) == 3

    with_internal = build_cohorts(cohort_dataset(), now=NOW, include_internal=True, since=None)
    assert {g["cohort"]: g["people"] for g in with_internal["groups"]}["AWARE"] == 3


def push_row(call_id: str, caller: str, started: datetime, result: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        parent_id="p1",
        child_id="c1",
        caller_id=caller,
        started_at=started,
        incoming_push={"result": result, "at": started.isoformat(), "devices": []},
    )


def test_not_rung_counts_only_calls_the_receiving_app_could_report() -> None:
    data = dataset()
    hours3 = NOW - timedelta(hours=3)
    data.users["i1"] = user("i1", "CHILD", NOW - timedelta(days=40))
    data.internal = {"i1"}
    data.pushes = [
        push_row("rang", "c1", hours3, "SENT"),
        push_row("silent", "c1", hours3, "SENT"),
        push_row("too-new", "c1", NOW - timedelta(minutes=10), "SENT"),
        push_row("expired", "c1", hours3, "TOKEN_INVALID"),
        push_row("to-child", "p1", hours3, "SENT"),
        SimpleNamespace(
            id="old-app", parent_id="p2", child_id="c2", caller_id="c2", started_at=hours3,
            incoming_push={"result": "SENT", "devices": []},
        ),
    ]
    data.first_event = {"p1": NOW - timedelta(days=1), "c1": hours3 + timedelta(minutes=1)}
    data.events = [
        app_event("p1", "incoming_call_shown", hours3, call_id="rang"),
        app_event("c1", "incoming_call_shown", hours3, call_id="silent"),  # 건 사람 기록은 아님
        app_event("c1", "incoming_call_shown", hours3, call_id="to-child"),
        app_event("c1", "call_connect_failed", NOW - timedelta(days=1), "NETWORK"),
        app_event("p1", "call_connect_failed", NOW - timedelta(days=10), "NETWORK"),
        app_event("c1", "call_connect_failed", NOW - timedelta(hours=2), "MIC_PERMISSION"),
        app_event("i1", "call_connect_failed", NOW - timedelta(hours=2), "SERVER"),
    ]
    delivery = build_delivery(data, now=NOW, include_internal=False, since=None)
    assert delivery["pushResults"] == {"SENT": 5, "TOKEN_INVALID": 1, "FAILED": 0, "NO_DEVICE": 0}
    # rang·silent·to-child 만 센다(too-new 는 1시간 전, old-app 은 받는 앱이 기록을 보낸 적 없음)
    assert delivery["notRung"] == {"n": 1, "d": 3, "pct": 33.3}
    assert delivery["connectFailed"] == {
        "all": {"NETWORK": 2, "MIC_PERMISSION": 1},
        "last7d": {"NETWORK": 1, "MIC_PERMISSION": 1},
    }


# ------------------------------------------------------------ 가입 퍼널


def step(user_id: str | None, code: str, platform: str, at: datetime) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, code=code, platform=platform, occurred_at=at)


def signup_dataset() -> Dataset:
    data = Dataset()
    start = NOW - timedelta(days=40)
    for uid, role in (("c1", "CHILD"), ("p1", "PARENT"), ("c2", "CHILD"), ("p2", "PARENT"),
                      ("i1", "CHILD")):
        data.users[uid] = user(uid, role, start)
    data.users["late"] = user("late", "CHILD", NOW - timedelta(days=1), nickname=None)
    data.internal = {"i1"}
    data.consent_latest = {"c1": "GRANTED", "p1": "GRANTED", "c2": "DENIED", "i1": "GRANTED",
                           "late": "GRANTED"}
    data.profiles = [SimpleNamespace(parent_id=uid, conditions=[], concerns=None)
                     for uid in ("p1", "p2", "c2")]
    data.inviters = {"c1", "c2", "i1"}
    data.family_users = [(start, {"c1", "p1"}), (start, {"c2"}), (start, {"p2", "i1"})]
    data.calls = [
        call("p1", "c1", NOW - timedelta(days=3)),
        call("p2", "i1", NOW - timedelta(days=3)),  # 팀·시험 계정과 한 통화
        call("p2", "c2", None),  # 안 받은 전화
    ]
    data.reports = [
        SimpleNamespace(parent_id="p1", issued_at=NOW, state="READY"),
        SimpleNamespace(parent_id="p2", issued_at=NOW, state="EMPTY"),
    ]
    data.onboarding = [
        step(None, "WELCOME", "IOS", NOW - timedelta(days=1)),
        step(None, "WELCOME", "IOS", NOW - timedelta(days=10)),
        step(None, "LOGIN", "ANDROID", NOW - timedelta(days=1)),
        step("late", "TERMS", "IOS", NOW - timedelta(days=1)),
        step("i1", "CONDITIONS", "ANDROID", NOW - timedelta(days=30)),
    ]
    return data


def test_signup_funnel_counts_each_stage_among_signed_up_people() -> None:
    signup = build_signup(signup_dataset(), now=NOW, include_internal=False, since=None)
    assert signup["signedUp"] == 4
    assert signup["loginOnly"] == 1
    assert signup["stages"] == {
        "consentGranted": {"n": 2, "d": 4, "pct": 50.0},
        "consentDenied": {"n": 1, "d": 4, "pct": 25.0},
        "conditions": {"n": 3, "d": 4, "pct": 75.0},
        "inviteCreated": {"n": 2, "d": 4, "pct": 50.0},
        # p2 의 가족은 팀·시험 계정 i1 을 빼면 혼자다
        "connected": {"n": 2, "d": 4, "pct": 50.0},
        "firstCall": {"n": 2, "d": 4, "pct": 50.0},
        # p1 리포트 → p1 과 통화한 c1 도 셈. 빈 리포트(p2)는 세지 않는다
        "firstReport": {"n": 2, "d": 4, "pct": 50.0},
    }
    steps = {row["step"]: row for row in signup["onboardingSteps"]}
    assert list(steps) == ["WELCOME", "LOGIN", "TERMS", "AI_CONSENT", "PROFILE", "CONDITIONS",
                           "INVITE_CODE", "DONE"]
    assert steps["WELCOME"] == {"step": "WELCOME", "IOS": 2, "ANDROID": 0, "anonymous": 2}
    assert steps["LOGIN"] == {"step": "LOGIN", "IOS": 0, "ANDROID": 1, "anonymous": 1}
    # 가입 단계에서 멈춘 계정의 로그인 뒤 기록은 센다. 팀·시험 계정 기록은 뺀다
    assert steps["TERMS"] == {"step": "TERMS", "IOS": 1, "ANDROID": 0, "anonymous": 0}
    assert steps["CONDITIONS"]["ANDROID"] == 0


def test_signup_funnel_follows_internal_and_since_filters() -> None:
    with_internal = build_signup(signup_dataset(), now=NOW, include_internal=True, since=None)
    assert with_internal["signedUp"] == 5
    assert with_internal["stages"]["inviteCreated"]["n"] == 3
    assert with_internal["stages"]["connected"]["n"] == 4
    assert with_internal["stages"]["firstCall"]["n"] == 4
    assert {r["step"]: r["ANDROID"] for r in with_internal["onboardingSteps"]}["CONDITIONS"] == 1

    recent = build_signup(
        signup_dataset(), now=NOW, include_internal=False, since=date(2026, 11, 20)
    )
    assert recent["signedUp"] == 0
    assert recent["stages"]["consentGranted"] == {"n": 0, "d": 0, "pct": None}
    steps = {row["step"]: row for row in recent["onboardingSteps"]}
    # 익명 줄은 기록 날짜로 거른다(10일 전 시작 화면 기록은 빠짐)
    assert steps["WELCOME"]["IOS"] == 1
    assert steps["TERMS"]["IOS"] == 1


def test_signup_funnel_reads_the_real_tables(ops_client: TestClient) -> None:
    onboard_family(ops_client)
    anonymous = ops_client.post("/v1/events/anonymous", json={"events": [{
        "id": str(uuid4()), "name": "onboarding_step", "at": datetime.now(UTC).isoformat(),
        "platform": "ANDROID", "appVersion": "1.2 (44)", "code": "WELCOME",
    }]})
    assert anonymous.json() == {"accepted": 1}
    signup = ops_client.get("/ops/api/summary", headers={"X-Ops-Code": CODE}).json()["signup"]
    assert signup["signedUp"] == 2
    assert signup["stages"]["consentGranted"]["n"] == 2
    assert signup["stages"]["inviteCreated"]["n"] == 1  # 초대한 자녀
    assert signup["stages"]["connected"]["n"] == 2
    assert signup["stages"]["firstCall"]["n"] == 0
    assert signup["onboardingSteps"][0] == {
        "step": "WELCOME", "IOS": 0, "ANDROID": 1, "anonymous": 1
    }


# ------------------------------------------------------------ 분석 품질


def quality_call(call_id: str, state: str, *, parent: str = "p", child: str = "c",
                 error: str | None = None, accepted: bool = True, recording: bool = True,
                 started: datetime | None = None) -> SimpleNamespace:
    item = call(parent, child, (started or NOW - timedelta(days=1)) if accepted else None, state)
    item.id = call_id
    item.processing_error = error
    item.recording_enabled = recording
    item.started_at = started or NOW - timedelta(days=1)
    return item


def pause(call_id: str, status: str, reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(call_id=call_id, status=status, unmeasurable_reason=reason)


def quality_dataset() -> Dataset:
    data = Dataset()
    start = NOW - timedelta(days=60)
    for uid, role in (("p", "PARENT"), ("c", "CHILD"), ("i", "CHILD")):
        data.users[uid] = user(uid, role, start)
    data.internal = {"i"}
    data.calls = [
        quality_call("a1", "ANALYZED"),
        quality_call("a2", "ANALYZED"),
        quality_call("a3", "ANALYZED"),
        quality_call("a4", "ANALYZED"),
        quality_call("x1", "ANALYSIS_EXCLUDED"),
        quality_call("x2", "ANALYSIS_EXCLUDED"),  # 동의 철회 등 다른 이유로 빠짐
        quality_call("f1", "ANALYSIS_FAILED", error="Analysis failed",
                     started=NOW - timedelta(days=40)),
        quality_call("f2", "ANALYSIS_FAILED", error="부모 Egress 녹음이 완료되지 않았습니다"),
        quality_call("f3", "ANALYSIS_FAILED", error="홍길동 님 통화 처리 실패"),
        quality_call("e1", "ENDED", error="Analysis retry pending"),
        quality_call("n1", "ANALYSIS_EXCLUDED", accepted=False),
        quality_call("r0", "ANALYSIS_EXCLUDED", recording=False),
        quality_call("i1", "ANALYZED", child="i"),  # 팀·시험 계정
    ]
    data.extraction_status = {"a1": "OK", "a2": "FAILED", "a4": "OK", "x1": "EXCLUDED",
                              "i1": "FAILED"}
    data.pause_ratio = {
        "a1": pause("a1", "OK"),
        "a2": pause("a2", "UNMEASURABLE", "NO_CHILD_TRACK"),
        "a3": pause("a3", "UNMEASURABLE", "DEFINITION_CHANGED"),
        "i1": pause("i1", "UNMEASURABLE", "NO_CHILD_TRACK"),
    }
    data.short_speech = {"x1", "r0", "i1"}
    return data


def test_analysis_quality_counts_and_hides_error_text() -> None:
    quality = build_quality(quality_dataset(), now=NOW, include_internal=False, since=None)
    assert quality["processingErrors"] == {
        "all": {"ANALYSIS_ERROR": 1, "PARENT_RECORDING_MISSING": 1, "OTHER": 1,
                "RETRY_PENDING": 1},
        "last30d": {"PARENT_RECORDING_MISSING": 1, "OTHER": 1, "RETRY_PENDING": 1},
    }
    assert "홍길동" not in json.dumps(quality, ensure_ascii=False)
    # a2 는 추출 실패, a3 는 추출 결과 줄 없음
    assert quality["analyzedWithoutExtraction"] == {"n": 2, "d": 4, "pct": 50.0}
    # 정의가 바뀌어 지운 a3 와 기록이 없는 a4 는 빼고 a1·a2 가운데
    assert quality["pauseUnmeasurable"] == {"n": 1, "d": 2, "pct": 50.0}
    assert quality["pauseUnmeasurableReasons"] == {"NO_CHILD_TRACK": 1}
    # 녹음된 통화(받음·녹음 켬) 10건 가운데 말이 짧아 빠진 x1
    assert quality["shortSpeechExcluded"] == {"n": 1, "d": 10, "pct": 10.0}

    with_internal = build_quality(quality_dataset(), now=NOW, include_internal=True, since=None)
    assert with_internal["analyzedWithoutExtraction"] == {"n": 3, "d": 5, "pct": 60.0}


def test_analysis_quality_reads_the_real_tables(ops_client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(ops_client)
    analyze_family_call(ops_client, child_token, parent_token, parent, LONG_TALK)
    analyze_family_call(
        ops_client, child_token, parent_token, parent, "PARENT: 응 잘 지내",
        state="ANALYSIS_EXCLUDED",
    )
    quality = ops_client.get("/ops/api/summary", headers={"X-Ops-Code": CODE}).json()["quality"]
    assert quality["analyzedWithoutExtraction"] == {"n": 0, "d": 1, "pct": 0.0}
    assert quality["pauseUnmeasurable"]["d"] == 1
    assert quality["shortSpeechExcluded"] == {"n": 1, "d": 2, "pct": 50.0}
    assert quality["processingErrors"] == {"all": {}, "last30d": {}}


# ------------------------------------------------------------ 건강 연동 결과


def test_health_link_counts_people_by_latest_result_and_every_event() -> None:
    data = Dataset()
    start = NOW - timedelta(days=40)
    for uid in ("a", "b", "i"):
        data.users[uid] = user(uid, "PARENT", start)
    data.internal = {"i"}
    data.health_results = [
        # a: 안드로이드에서 거부했다가 연결 → 사람은 연결 1, 건수는 둘 다
        step("a", "PERMISSION_DENIED", "ANDROID", NOW - timedelta(days=3)),
        step("a", "CONNECTED", "ANDROID", NOW - timedelta(days=1)),
        step("b", "CONNECTED_NO_RECORDS", "IOS", NOW - timedelta(days=2)),
        step("b", "CONNECTED_NO_RECORDS", "IOS", NOW - timedelta(days=1)),
        step("i", "CONNECTED", "IOS", NOW - timedelta(days=1)),  # 팀·시험 계정
    ]
    health = build_health_link(data, now=NOW, include_internal=False, since=None)
    assert health["people"] == 2
    assert health["events"] == 4
    assert health["results"] == [
        {"code": "CONNECTED", "people": {"IOS": 0, "ANDROID": 1},
         "events": {"IOS": 0, "ANDROID": 1}},
        {"code": "PERMISSION_DENIED", "people": {"IOS": 0, "ANDROID": 0},
         "events": {"IOS": 0, "ANDROID": 1}},
        {"code": "CONNECTED_NO_RECORDS", "people": {"IOS": 1, "ANDROID": 0},
         "events": {"IOS": 2, "ANDROID": 0}},
    ]
    with_internal = build_health_link(data, now=NOW, include_internal=True, since=None)
    assert with_internal["results"][0]["people"] == {"IOS": 1, "ANDROID": 1}


def test_health_link_reads_the_real_events(ops_client: TestClient) -> None:
    _, _, parent_token, _ = onboard_family(ops_client)
    sent = ops_client.post("/v1/events", headers=auth(parent_token), json={"events": [
        {"id": str(uuid4()), "name": "health_link_result", "code": code,
         "at": (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(),
         "platform": "ANDROID", "appVersion": "1.2 (44)"}
        for code, minutes in (("PERMISSION_DENIED", 5), ("CONNECTED", 1))
    ]})
    assert sent.json() == {"accepted": 2}
    health = ops_client.get("/ops/api/summary", headers={"X-Ops-Code": CODE}).json()["healthLink"]
    assert health["people"] == 1
    assert health["events"] == 2
    assert health["results"][0]["people"] == {"IOS": 0, "ANDROID": 1}
