from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.models import CallRecord
from tests.conftest import auth
from tests.test_api_flow import onboard_family


def test_unrecorded_call_finishes_and_allows_account_deletion(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    document = client.get("/v1/consents/document").json()
    denied = client.post("/v1/consents", headers=auth(child_token), json={
        "documentVersion": document["version"], "decision": "DENY",
        "scrolledToEnd": False, "agreedItems": [],
    })
    assert denied.status_code == 201
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    call_id = created.json()["callId"]
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 200
    assert client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token)).status_code == 200
    state = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert state["state"] == "ANALYSIS_EXCLUDED"
    assert state["recordingEnabled"] is False
    assert client.delete("/v1/account", headers=auth(parent_token)).status_code == 204


def test_maintenance_repairs_old_unrecorded_ended_calls(client: TestClient) -> None:
    _, child, _, parent = onboard_family(client)
    container = client.app.state.container

    async def seed() -> str:
        now = datetime.now(UTC)
        async with container.database.sessions() as session:
            call = CallRecord(
                parent_id=parent["id"], child_id=child["id"], room_name="old-unrecorded",
                state="ENDED", recording_enabled=False,
                started_at=now - timedelta(minutes=2), ended_at=now, room_closed_at=now,
            )
            session.add(call)
            await session.commit()
            return call.id

    call_id = client.portal.call(seed)
    client.portal.call(container.calls.maintain)

    async def check() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            assert call.state == "ANALYSIS_EXCLUDED"
            assert call.processing_claimed_at is None
            assert call.ended_at is not None

    client.portal.call(check)
