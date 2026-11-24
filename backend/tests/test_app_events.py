from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.database import Base, Database
from app.models import AppEvent
from app.services import events as event_rules
from app.services.events import EventRetention
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family


def event(
    name: str,
    *,
    code: str | None = None,
    subject: str | None = None,
    call: str | None = None,
    at: datetime | None = None,
    event_id: str | None = None,
) -> dict:
    return {
        "id": event_id or str(uuid4()),
        "name": name,
        "at": (at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "platform": "IOS",
        "appVersion": "1.2 (44)",
        "code": code,
        "subjectId": subject,
        "callId": call,
    }


def stored(tmp_path: Path) -> dict[str, tuple]:
    with sqlite3.connect(tmp_path / "test.db") as connection:
        rows = connection.execute(
            "select id, user_id, name, code, subject_id, call_id, platform, app_version"
            " from app_events"
        ).fetchall()
    return {row[0]: row[1:] for row in rows}


def declined_call(client: TestClient, child_token: str, parent_token: str, parent_id: str) -> str:
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent_id})
    assert created.status_code == 201, created.text
    call_id = created.json()["callId"]
    declined = client.post(f"/v1/calls/{call_id}/decline", headers=auth(parent_token))
    assert declined.status_code == 200, declined.text
    return call_id


def test_events_are_recorded_once_per_id(client: TestClient, tmp_path: Path) -> None:
    child_token, child, _, parent = onboard_family(client)
    batch = [
        event("app_open"),
        event("report_view", code="DIRECT", subject=parent["id"]),
        event("health_link_result", code="CONNECTED_NO_RECORDS"),
        event("metric_help_open", code="SPEECH_RATE"),
        event("onboarding_step", code="PROFILE"),
    ]
    response = client.post("/v1/events", headers=auth(child_token), json={"events": batch})
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 5}

    # 다시 보내도(같은 id) 한 번만 남고, 같은 묶음 안의 중복도 하나로 센다
    again = client.post(
        "/v1/events", headers=auth(child_token), json={"events": batch + batch[:1]}
    )
    assert again.json() == {"accepted": 5}
    rows = stored(tmp_path)
    assert len(rows) == 5
    view = rows[batch[1]["id"]]
    assert view == (child["id"], "report_view", "DIRECT", parent["id"], None, "IOS", "1.2 (44)")


def test_unknown_old_and_future_events_are_dropped(client: TestClient, tmp_path: Path) -> None:
    token, _ = create_user(client, "01055556666", "CHILD", "시험")
    now = datetime.now(UTC)
    kept = event("app_open", at=now - timedelta(days=29))
    batch = [
        kept,
        event("screen_view"),
        event("report_view", code="SHARE"),
        event("report_view"),
        event("app_open", code="DIRECT"),
        event("metric_help_open", code="speech-rate"),
        event("app_open", at=now - timedelta(days=31)),
        event("app_open", at=now + timedelta(minutes=6)),
    ]
    response = client.post("/v1/events", headers=auth(token), json={"events": batch})
    assert response.status_code == 200
    assert response.json() == {"accepted": 1}
    assert set(stored(tmp_path)) == {kept["id"]}


def test_subject_and_call_kept_only_for_family_and_own_calls(
    client: TestClient, tmp_path: Path
) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    call_id = declined_call(client, child_token, parent_token, parent["id"])
    stranger_token, stranger = create_user(client, "01077778888", "CHILD", "남")

    answer = event("post_call_answer", code="YES", subject=parent["id"], call=call_id)
    self_view = event("report_view", code="NOTIFICATION", subject=child["id"])
    foreign_view = event("report_view", code="DIRECT", subject=stranger["id"])
    stray = event("app_open", subject=parent["id"], call=call_id)
    response = client.post(
        "/v1/events",
        headers=auth(child_token),
        json={"events": [answer, self_view, foreign_view, stray]},
    )
    assert response.json() == {"accepted": 4}

    shown = event("incoming_call_shown", call=call_id)
    peeking = event("call_connect_failed", code="NETWORK", call=call_id)
    client.post("/v1/events", headers=auth(parent_token), json={"events": [shown]})
    client.post("/v1/events", headers=auth(stranger_token), json={"events": [peeking]})

    rows = stored(tmp_path)
    assert rows[answer["id"]][3:5] == (parent["id"], call_id)
    assert rows[self_view["id"]][3] == child["id"]
    assert rows[foreign_view["id"]][3] is None
    assert rows[stray["id"]][3:5] == (None, None)
    assert rows[shown["id"]][4] == call_id
    assert rows[peeking["id"]][0] == stranger["id"]
    assert rows[peeking["id"]][4] is None


def test_events_need_login_and_a_valid_body(client: TestClient) -> None:
    token, _ = create_user(client, "01055556666", "CHILD", "시험")
    assert client.post("/v1/events", json={"events": [event("app_open")]}).status_code == 401
    too_many = [event("app_open") for _ in range(51)]
    assert client.post(
        "/v1/events", headers=auth(token), json={"events": too_many}
    ).status_code == 422
    for broken in (
        {**event("app_open"), "platform": "WEB"},
        {**event("app_open"), "id": "not-a-uuid"},
        {**event("app_open"), "appVersion": "1" * 41},
    ):
        response = client.post("/v1/events", headers=auth(token), json={"events": [broken]})
        assert response.status_code == 422
    empty = client.post("/v1/events", headers=auth(token), json={"events": []})
    assert empty.json() == {"accepted": 0}


def test_anonymous_events_take_only_onboarding_steps(client: TestClient, tmp_path: Path) -> None:
    step = {**event("onboarding_step", code="WELCOME"), "subjectId": "x", "callId": "y"}
    batch = [step, event("app_open"), event("onboarding_step", code="SECRET")]
    response = client.post("/v1/events/anonymous", json={"events": batch})
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 1}
    assert stored(tmp_path) == {
        step["id"]: (None, "onboarding_step", "WELCOME", None, None, "IOS", "1.2 (44)")
    }

    too_many = [event("onboarding_step", code="LOGIN") for _ in range(21)]
    assert client.post("/v1/events/anonymous", json={"events": too_many}).status_code == 422
    assert client.post(
        "/v1/events/anonymous", content=b"{", headers={"content-type": "application/json"}
    ).status_code == 422

    padded = b'{"events": [], "pad": "' + b"x" * 20_000 + b'"}'
    assert client.post("/v1/events/anonymous", content=padded).status_code == 413
    chunks = (part for part in [padded[:10_000], padded[10_000:]])
    assert client.post("/v1/events/anonymous", content=chunks).status_code == 413


def test_anonymous_events_stop_at_the_window_limit(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(event_rules, "ANONYMOUS_WINDOW_LIMIT", 3)
    first = [event("onboarding_step", code="WELCOME") for _ in range(2)]
    second = [event("onboarding_step", code="LOGIN") for _ in range(2)]
    assert client.post("/v1/events/anonymous", json={"events": first}).json() == {"accepted": 2}
    assert client.post("/v1/events/anonymous", json={"events": second}).json() == {"accepted": 1}
    assert len(stored(tmp_path)) == 3


def test_events_show_in_openapi(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    for path in ("/v1/events", "/v1/events/anonymous"):
        assert document["paths"][path]["post"]["tags"] == ["Events"]
    anonymous = document["paths"]["/v1/events/anonymous"]["post"]["requestBody"]
    items = anonymous["content"]["application/json"]["schema"]["properties"]["events"]["items"]
    assert items["$ref"].rsplit("/", 1)[-1] in document["components"]["schemas"]


async def test_old_events_are_removed_after_a_year(tmp_path: Path) -> None:
    # 앱을 띄우면 정기 작업이 같은 정리를 돌리므로, 정기 작업이 없는 DB 에서 본다
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC)
    rows = [
        {"id": name, "user_id": None, "name": "app_open", "platform": "IOS",
         "app_version": "1.0 (1)", "occurred_at": now - age, "received_at": now - age}
        for name, age in (("old", timedelta(days=366)), ("recent", timedelta(days=364)))
    ]
    try:
        async with database.sessions() as session:
            await session.execute(insert(AppEvent), rows)
            await session.commit()
        retention = EventRetention(database)
        assert await retention.maintain() == 1
        # 한 시간 안에 다시 불러도 돌지 않는다
        assert await retention.maintain() == 0
        async with database.sessions() as session:
            assert set(await session.scalars(select(AppEvent.id))) == {"recent"}
    finally:
        await database.close()


def test_deleting_an_account_removes_its_events(client: TestClient, tmp_path: Path) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    call_id = declined_call(client, child_token, parent_token, parent["id"])
    mine = [
        event("app_open"),
        event("post_call_answer", code="NO", subject=parent["id"], call=call_id),
    ]
    theirs_view = event("report_view", code="DIRECT", subject=child["id"])
    theirs_shown = event("incoming_call_shown", call=call_id)
    theirs_open = event("app_open")
    client.post("/v1/events", headers=auth(child_token), json={"events": mine})
    client.post(
        "/v1/events",
        headers=auth(parent_token),
        json={"events": [theirs_view, theirs_shown, theirs_open]},
    )
    assert len(stored(tmp_path)) == 5

    assert client.delete("/v1/account", headers=auth(child_token)).status_code == 204
    rows = stored(tmp_path)
    assert set(rows) == {theirs_view["id"], theirs_shown["id"], theirs_open["id"]}
    assert not [row for row in rows.values() if row[0] == child["id"]]
    assert rows[theirs_view["id"]][0] == parent["id"]
    assert rows[theirs_view["id"]][3] is None
    assert rows[theirs_shown["id"]][4] is None


def test_signed_in_events_stop_at_the_daily_limit_per_person(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(event_rules, "USER_WINDOW_LIMIT", 3)
    child_token, child, parent_token, _ = onboard_family(client)

    def send(token: str, count: int) -> dict:
        batch = [event("app_open") for _ in range(count)]
        response = client.post("/v1/events", headers=auth(token), json={"events": batch})
        assert response.status_code == 200, response.text
        return response.json()

    assert send(child_token, 2) == {"accepted": 2}
    assert send(child_token, 2) == {"accepted": 1}  # 넘친 줄은 조용히 버린다
    assert send(child_token, 1) == {"accepted": 0}
    assert send(parent_token, 2) == {"accepted": 2}  # 사람마다 따로 센다

    # 받은 지 24시간이 지난 줄은 세지 않는다
    with sqlite3.connect(tmp_path / "test.db") as connection:
        connection.execute(
            "update app_events set received_at = ? where user_id = ?",
            # SQLAlchemy 가 SQLite 에 쓰는 꼴(공백 구분)과 같게 둔다
            (f"{datetime.now(UTC) - timedelta(hours=25):%Y-%m-%d %H:%M:%S.%f}", child["id"]),
        )
    assert send(child_token, 2) == {"accepted": 2}


def test_event_limits_are_the_agreed_numbers() -> None:
    assert event_rules.USER_WINDOW == timedelta(hours=24)
    assert event_rules.USER_WINDOW_LIMIT == 500
    assert event_rules.ANONYMOUS_WINDOW == timedelta(minutes=10)
    assert event_rules.ANONYMOUS_WINDOW_LIMIT == 200


def test_rows_with_a_bad_app_version_or_odd_ids_are_dropped_not_the_batch(
    client: TestClient, tmp_path: Path
) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    call_id = declined_call(client, child_token, parent_token, parent["id"])
    good = event("app_open")
    versions = [
        {**event("app_open"), "appVersion": version}
        for version in ("1.2", "1.2(44)", "v1.2 (44)", "1.2 (44) beta", "1.2 (4\u00004)",
                        "1.2.3.4.5 (1)", "12345 (1)", "1.2 (1234567890)")
    ]
    nul_view = event("report_view", code="DIRECT", subject=f"{parent['id']}\u0000")
    nul_shown = event("incoming_call_shown", call=f"{call_id}\u0000")
    empty_view = {**event("report_view", code="DIRECT"), "subjectId": ""}
    nul_name = {**event("app_open"), "name": "app_open\u0000"}
    nul_code = event("report_view", code="DIRECT\u0000")
    batch = [good, *versions, nul_view, nul_shown, empty_view, nul_name, nul_code]
    response = client.post("/v1/events", headers=auth(child_token), json={"events": batch})
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 4}
    rows = stored(tmp_path)
    assert set(rows) == {good["id"], nul_view["id"], nul_shown["id"], empty_view["id"]}
    assert rows[nul_view["id"]][3] is None
    assert rows[nul_shown["id"]][4] is None
    assert rows[empty_view["id"]][3] is None

    # 꼴이 맞는 여러 판
    ok = [{**event("app_open"), "appVersion": v} for v in ("1 (1)", "1.2.3.4 (123456789)")]
    assert client.post(
        "/v1/events", headers=auth(child_token), json={"events": ok}
    ).json() == {"accepted": 2}


def test_empty_subject_and_call_ids_count_as_missing(client: TestClient, tmp_path: Path) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    call_id = declined_call(client, child_token, parent_token, parent["id"])
    view = {**event("report_view", code="DIRECT"), "subjectId": ""}
    shown = {**event("incoming_call_shown"), "callId": ""}
    answer = event("post_call_answer", code="YES", subject="", call=call_id)
    client.post("/v1/events", headers=auth(child_token), json={"events": [view, shown, answer]})
    rows = stored(tmp_path)
    assert rows[view["id"]][3:5] == (None, None)
    assert rows[shown["id"]][3:5] == (None, None)
    assert rows[answer["id"]][3:5] == (None, call_id)


def test_platform_takes_only_the_two_app_values(client: TestClient) -> None:
    token, _ = create_user(client, "01055556666", "CHILD", "시험")
    for platform in ("ios", "Android", "IOS\u0000", "", "WEB"):
        response = client.post(
            "/v1/events", headers=auth(token),
            json={"events": [{**event("app_open"), "platform": platform}]},
        )
        assert response.status_code == 422, platform
    for platform in ("IOS", "ANDROID"):
        response = client.post(
            "/v1/events", headers=auth(token),
            json={"events": [{**event("app_open"), "platform": platform}]},
        )
        assert response.json() == {"accepted": 1}


def test_call_answers_need_a_call_of_the_person_who_answered(
    client: TestClient, tmp_path: Path
) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    call_id = declined_call(client, child_token, parent_token, parent["id"])
    stranger_token, _ = create_user(client, "01077778888", "CHILD", "남")
    own = event("post_call_answer", code="YES", subject=parent["id"], call=call_id)
    no_call = event("post_call_answer", code="NO", subject=parent["id"])
    unknown_call = event("post_call_answer", code="NO", call="no-such-call")
    response = client.post(
        "/v1/events", headers=auth(child_token), json={"events": [own, no_call, unknown_call]}
    )
    assert response.json() == {"accepted": 1}
    foreign = event("post_call_answer", code="YES", call=call_id)
    assert client.post(
        "/v1/events", headers=auth(stranger_token), json={"events": [foreign]}
    ).json() == {"accepted": 0}
    assert set(stored(tmp_path)) == {own["id"]}


def test_metric_help_code_takes_no_digit_runs(client: TestClient) -> None:
    token, _ = create_user(client, "01055556666", "CHILD", "시험")
    kept = ("PAUSE_RATIO", "F0_VARIATION", "A" * 40)
    dropped = ("01012345678", "P01012345678", "PAUSE_RATIO2", "F00_VARIATION", "SPEECH RATE",
               "pause_ratio", "A" * 41)
    batch = [event("metric_help_open", code=code) for code in kept + dropped]
    response = client.post("/v1/events", headers=auth(token), json={"events": batch})
    assert response.json() == {"accepted": len(kept)}
