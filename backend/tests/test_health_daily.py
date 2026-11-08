from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.consent import CONSENT_VERSION, HEALTH_LINK_ITEMS, HEALTH_LINK_VERSION
from app.models import HealthDailySummary, HealthLinkConsent
from app.services.reports import REPORT_TIMEZONE
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_family_sharing import put_sharing


def today() -> date:
    return datetime.now(REPORT_TIMEZONE).date()


def day(offset: int, **values) -> dict:
    return {"date": (today() - timedelta(days=offset)).isoformat(), **values}


def link(client: TestClient, token: str, **headers: str):
    return client.post(
        "/v1/health/link",
        headers={**auth(token), **headers},
        json={"version": HEALTH_LINK_VERSION, "platform": "HEALTH_CONNECT"},
    )


def upload(client: TestClient, token: str, *days: dict):
    return client.put(
        "/v1/health/daily",
        headers=auth(token),
        json={"platform": "HEALTH_CONNECT", "days": list(days)},
    )


def daily(client: TestClient, token: str, subject_id: str, start: date, end: date):
    return client.get(
        f"/v1/parents/{subject_id}/health/daily",
        headers=auth(token),
        params={"from": start.isoformat(), "to": end.isoformat()},
    )


def rows(client: TestClient, model, user_id: str) -> list:
    async def run() -> list:
        async with client.app.state.container.database.sessions() as session:
            return list(await session.scalars(
                select(model).where(model.user_id == user_id)
            ))

    return client.portal.call(run)


def test_link_shows_document_and_records_optional_consent(client: TestClient) -> None:
    _, _, parent_token, parent = onboard_family(client)

    before = client.get("/v1/health/link", headers=auth(parent_token))
    assert before.status_code == 200, before.text
    assert before.json() == {
        "connected": False,
        "consentVersion": None,
        "connectedAt": None,
        "document": {
            "version": "health-link-2026-11-08.v1",
            "title": "건강 앱 연결 (선택)",
            "items": HEALTH_LINK_ITEMS,
        },
        "lastSyncedDay": None,
    }
    assert len(HEALTH_LINK_ITEMS) == 6

    stale = client.post(
        "/v1/health/link",
        headers=auth(parent_token),
        json={"version": "health-link-old", "platform": "HEALTH_CONNECT"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "HEALTH_CONSENT_VERSION_MISMATCH"
    unknown = client.post(
        "/v1/health/link",
        headers=auth(parent_token),
        json={"version": HEALTH_LINK_VERSION, "platform": "FITBIT"},
    )
    assert unknown.status_code == 422

    headers = {"User-Agent": "CollogAndroid/1.0", "X-Forwarded-For": "203.0.113.7"}
    linked = link(client, parent_token, **headers)
    assert linked.status_code == 200, linked.text
    body = linked.json()
    assert body["connected"] is True
    assert body["consentVersion"] == HEALTH_LINK_VERSION
    assert body["connectedAt"] is not None
    assert body["lastSyncedDay"] is None
    assert client.get("/v1/health/link", headers=auth(parent_token)).json() == body

    [record] = rows(client, HealthLinkConsent, parent["id"])
    assert (record.decision, record.platform, record.document_version) == (
        "GRANTED", "HEALTH_CONNECT", HEALTH_LINK_VERSION,
    )
    assert (record.ip, record.user_agent) == ("203.0.113.7", "CollogAndroid/1.0")
    # 필수 동의(v5)는 그대로다.
    required = client.get("/v1/consents/me", headers=auth(parent_token)).json()
    assert required["documentVersion"] == CONSENT_VERSION
    assert required["isCurrent"] is True


def test_upload_requires_link_and_replaces_each_day(client: TestClient) -> None:
    _, _, parent_token, parent = onboard_family(client)
    refused = upload(client, parent_token, day(1, steps=100))
    assert refused.status_code == 403
    assert refused.json()["code"] == "HEALTH_LINK_REQUIRED"
    assert rows(client, HealthDailySummary, parent["id"]) == []

    assert link(client, parent_token).status_code == 200
    first = upload(
        client,
        parent_token,
        day(
            2, sleepMinutes=370, steps=3210, restingHeartRate=64,
            bloodPressureSystolic=128, bloodPressureDiastolic=82, bloodGlucoseMgDl=112.0,
        ),
        day(1, steps=100),
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"saved": 2, "lastSyncedDay": day(1)["date"]}

    # 같은 날을 다시 보내면 그날 값 전체를 새 값으로 바꾼다(없는 값은 null).
    second = upload(client, parent_token, day(2, sleepMinutes=400), day(0, steps=50))
    assert second.json() == {"saved": 2, "lastSyncedDay": day(0)["date"]}
    view = daily(client, parent_token, parent["id"], today() - timedelta(days=6), today())
    assert view.status_code == 200, view.text
    assert view.json()["days"] == [
        {
            "date": day(2)["date"], "sleepMinutes": 400, "steps": None,
            "restingHeartRate": None, "bloodPressureSystolic": None,
            "bloodPressureDiastolic": None, "bloodGlucoseMgDl": None,
        },
        {
            "date": day(1)["date"], "sleepMinutes": None, "steps": 100,
            "restingHeartRate": None, "bloodPressureSystolic": None,
            "bloodPressureDiastolic": None, "bloodGlucoseMgDl": None,
        },
        {
            "date": day(0)["date"], "sleepMinutes": None, "steps": 50,
            "restingHeartRate": None, "bloodPressureSystolic": None,
            "bloodPressureDiastolic": None, "bloodGlucoseMgDl": None,
        },
    ]
    assert len(rows(client, HealthDailySummary, parent["id"])) == 3
    status = client.get("/v1/health/link", headers=auth(parent_token)).json()
    assert status["lastSyncedDay"] == day(0)["date"]


def test_upload_rejects_out_of_range_values(client: TestClient) -> None:
    _, _, parent_token, _ = onboard_family(client)
    assert link(client, parent_token).status_code == 200
    rejected = {
        "no days": [],
        "too many days": [day(offset) for offset in range(36)],
        "future": [day(-1)],
        "too old": [day(401)],
        "duplicate": [day(1), day(1)],
        "sleep low": [day(1, sleepMinutes=-1)],
        "sleep high": [day(1, sleepMinutes=1441)],
        "steps low": [day(1, steps=-1)],
        "steps high": [day(1, steps=100_001)],
        "heart low": [day(1, restingHeartRate=24)],
        "heart high": [day(1, restingHeartRate=221)],
        "systolic low": [day(1, bloodPressureSystolic=49)],
        "systolic high": [day(1, bloodPressureSystolic=261)],
        "diastolic low": [day(1, bloodPressureDiastolic=29)],
        "diastolic high": [day(1, bloodPressureDiastolic=201)],
        "diastolic not below": [day(1, bloodPressureSystolic=120, bloodPressureDiastolic=120)],
        "glucose low": [day(1, bloodGlucoseMgDl=19.9)],
        "glucose high": [day(1, bloodGlucoseMgDl=600.1)],
    }
    for name, days in rejected.items():
        response = upload(client, parent_token, *days)
        assert response.status_code == 422, (name, response.text)
        assert response.json()["code"] == "VALIDATION_ERROR", name

    accepted = upload(
        client,
        parent_token,
        *[day(offset) for offset in range(35)],
    )
    assert accepted.status_code == 200, accepted.text
    edges = upload(
        client,
        parent_token,
        day(
            400, sleepMinutes=0, steps=0, restingHeartRate=25,
            bloodPressureSystolic=50, bloodPressureDiastolic=30, bloodGlucoseMgDl=20,
        ),
        day(
            0, sleepMinutes=1440, steps=100_000, restingHeartRate=220,
            bloodPressureSystolic=260, bloodPressureDiastolic=200, bloodGlucoseMgDl=600,
        ),
    )
    assert edges.status_code == 200, edges.text


def test_family_view_follows_report_sharing(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    assert link(client, parent_token).status_code == 200
    assert upload(client, parent_token, day(1, sleepMinutes=412, steps=5120)).status_code == 200
    week = (today() - timedelta(days=6), today())

    seen = daily(client, child_token, parent["id"], *week)
    assert seen.status_code == 200, seen.text
    assert seen.json() == {
        "parentId": parent["id"],
        "connected": True,
        "platform": "HEALTH_CONNECT",
        "lastSyncedDay": day(1)["date"],
        "days": [
            {
                "date": day(1)["date"], "sleepMinutes": 412, "steps": 5120,
                "restingHeartRate": None, "bloodPressureSystolic": None,
                "bloodPressureDiastolic": None, "bloodGlucoseMgDl": None,
            }
        ],
        "disclaimer": "이 정보는 의료 진단이 아니며 건강 변화 참고용입니다.",
    }

    stranger_token, _ = create_user(client, "01040000009", "CHILD", "남")
    assert daily(client, stranger_token, parent["id"], *week).status_code == 403

    assert put_sharing(client, parent_token, child["id"], report=False, timeline=True) == 200
    assert daily(client, child_token, parent["id"], *week).status_code == 403
    assert daily(client, parent_token, parent["id"], *week).status_code == 200
    assert put_sharing(client, parent_token, child["id"], report=True, timeline=False) == 200
    assert daily(client, child_token, parent["id"], *week).status_code == 200

    end = today()
    for start, stop, expected in (
        (end - timedelta(days=61), end, 200),
        (end - timedelta(days=62), end, 422),
        (end, end - timedelta(days=1), 422),
    ):
        assert daily(client, child_token, parent["id"], start, stop).status_code == expected

    # 연결하지 않은 사람은 빈 배열이다.
    unlinked = daily(client, parent_token, child["id"], *week)
    assert unlinked.status_code == 200
    assert unlinked.json()["connected"] is False
    assert unlinked.json()["platform"] is None
    assert unlinked.json()["lastSyncedDay"] is None
    assert unlinked.json()["days"] == []


def test_revoking_deletes_summaries_immediately(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    assert link(client, parent_token).status_code == 200
    assert upload(client, parent_token, day(2, steps=1), day(1, steps=2)).status_code == 200
    assert len(rows(client, HealthDailySummary, parent["id"])) == 2

    assert client.delete("/v1/health/link", headers=auth(parent_token)).status_code == 204
    assert rows(client, HealthDailySummary, parent["id"]) == []
    decisions = [
        item.decision
        for item in sorted(rows(client, HealthLinkConsent, parent["id"]), key=lambda r: r.agreed_at)
    ]
    assert decisions == ["GRANTED", "REVOKED"]
    status = client.get("/v1/health/link", headers=auth(parent_token)).json()
    assert status["connected"] is False
    assert status["lastSyncedDay"] is None
    assert upload(client, parent_token, day(1, steps=3)).status_code == 403
    view = daily(client, child_token, parent["id"], today() - timedelta(days=6), today())
    assert view.json()["connected"] is False
    assert view.json()["days"] == []

    # 이미 끊긴 상태에서 다시 끊어도 이력은 늘지 않는다.
    assert client.delete("/v1/health/link", headers=auth(parent_token)).status_code == 204
    assert len(rows(client, HealthLinkConsent, parent["id"])) == 2

    relinked = link(client, parent_token).json()
    assert relinked["connected"] is True
    assert relinked["lastSyncedDay"] is None


def test_account_deletion_removes_health_data(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    assert link(client, parent_token).status_code == 200
    assert link(client, child_token).status_code == 200
    assert upload(client, parent_token, day(1, steps=10)).status_code == 200
    assert upload(client, child_token, day(1, steps=20)).status_code == 200

    assert client.delete("/v1/account", headers=auth(parent_token)).status_code == 204
    assert rows(client, HealthDailySummary, parent["id"]) == []
    assert rows(client, HealthLinkConsent, parent["id"]) == []
    # 다른 사람의 요약은 남는다.
    assert len(rows(client, HealthDailySummary, child["id"])) == 1

    async def total() -> int:
        async with client.app.state.container.database.sessions() as session:
            return await session.scalar(select(func.count()).select_from(HealthDailySummary))

    assert client.portal.call(total) == 1
