from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

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


def test_old_events_are_removed_after_a_year(client: TestClient) -> None:
    container = client.app.state.container
    now = datetime.now(UTC)
    # 앱의 정기 작업이 시험 도중 먼저 지우지 않게 막아 둔다
    container.event_retention._last_run = now
    rows = [
        {"id": name, "user_id": None, "name": "app_open", "platform": "IOS",
         "app_version": "1.0 (1)", "occurred_at": now - age, "received_at": now - age}
        for name, age in (("old", timedelta(days=366)), ("recent", timedelta(days=364)))
    ]

    async def run() -> tuple[int, int, set[str]]:
        async with container.database.sessions() as session:
            await session.execute(insert(AppEvent), rows)
            await session.commit()
        retention = EventRetention(container.database)
        removed = await retention.maintain()
        # 한 시간 안에 다시 불러도 돌지 않는다
        skipped = await retention.maintain()
        async with container.database.sessions() as session:
            left = set(await session.scalars(select(AppEvent.id)))
        return removed, skipped, left

    removed, skipped, left = client.portal.call(run)
    assert removed == 1
    assert skipped == 0
    assert left == {"recent"}


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
