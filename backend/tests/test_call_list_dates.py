from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from app.models import CallRecord, CallState
from tests.conftest import auth
from tests.test_api_flow import onboard_family

# 2026-11-16 은 월요일이다. 한국 시간 새벽 3시는 UTC 로 전날(일요일) 18시다.
MONDAY = date(2026, 11, 16)
DAWN_KST_IN_UTC = datetime(2026, 11, 15, 18, 0, tzinfo=UTC)


def seed_call(client: TestClient, parent_id: str, child_id: str, started: datetime) -> str:
    async def insert() -> str:
        async with client.app.state.container.database.sessions() as session:
            call = CallRecord(
                parent_id=parent_id,
                child_id=child_id,
                room_name=f"dates-{started.isoformat()}",
                state=CallState.ANALYZED.value,
                started_at=started,
                ended_at=started + timedelta(minutes=3),
                duration_sec=180,
            )
            session.add(call)
            await session.commit()
            return call.id

    return client.portal.call(insert)


def listed(client: TestClient, token: str, parent_id: str, start: date, end: date) -> set[str]:
    response = client.get(
        f"/v1/parents/{parent_id}/calls",
        params={"from": start.isoformat(), "to": end.isoformat()},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return {call["callId"] for call in response.json()["calls"]}


def test_dawn_call_is_listed_in_the_same_korean_week_as_the_report(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)
    call_id = seed_call(client, parent["id"], child["id"], DAWN_KST_IN_UTC)

    sunday = MONDAY + timedelta(days=6)
    report = client.get(
        f"/v1/parents/{parent['id']}/reports",
        params={"period": "WEEKLY", "date": MONDAY.isoformat()},
        headers=auth(child_token),
    )
    assert report.status_code == 200, report.text
    assert report.json()["analyzedCallCount"] == 1

    # 앱은 리포트와 같은 주(한국 날짜 월~일)를 from·to 로 보낸다.
    assert call_id in listed(client, child_token, parent["id"], MONDAY, sunday)
    previous_monday = MONDAY - timedelta(days=7)
    assert call_id not in listed(
        client, child_token, parent["id"], previous_monday, MONDAY - timedelta(days=1)
    )


def test_late_evening_call_stays_on_its_korean_day(client: TestClient) -> None:
    # 한국 시간 일요일 23시 = UTC 일요일 14시. 다음 주 목록에 들어가면 안 된다.
    child_token, child, _, parent = onboard_family(client)
    sunday_night = datetime(2026, 11, 22, 14, 0, tzinfo=UTC)
    call_id = seed_call(client, parent["id"], child["id"], sunday_night)

    assert call_id in listed(client, child_token, parent["id"], MONDAY, MONDAY + timedelta(days=6))
    next_monday = MONDAY + timedelta(days=7)
    assert call_id not in listed(
        client, child_token, parent["id"], next_monday, next_monday + timedelta(days=6)
    )
