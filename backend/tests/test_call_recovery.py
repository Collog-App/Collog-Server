from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import run_maintenance
from app.models import CallRecord
from app.services.livekit import LiveKitError
from tests.conftest import auth
from tests.test_api_flow import onboard_family


def create_call(client: TestClient) -> tuple[str, str, str]:
    child_token, _, parent_token, parent = onboard_family(client)
    response = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert response.status_code == 201
    return response.json()["callId"], child_token, parent_token


def test_decline_closes_room_and_preserves_zero_duration(client: TestClient) -> None:
    call_id, child_token, parent_token = create_call(client)
    closed = []

    async def close_room(room_name: str) -> None:
        closed.append(room_name)

    client.app.state.container.livekit.delete_room = close_room
    declined = client.post(f"/v1/calls/{call_id}/decline", headers=auth(parent_token))
    assert declined.status_code == 200
    result = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert result["state"] == "ANALYSIS_EXCLUDED"
    assert result["durationSec"] == 0
    assert len(closed) == 1


def test_room_cleanup_retries_after_provider_failure(client: TestClient) -> None:
    call_id, child_token, _ = create_call(client)
    attempts = []

    async def close_room(room_name: str) -> None:
        attempts.append(room_name)
        if len(attempts) == 1:
            raise LiveKitError("temporary failure")

    container = client.app.state.container
    container.livekit.delete_room = close_room
    assert client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token)).status_code == 200
    client.portal.call(container.calls.maintain)
    assert len(attempts) == 2

    async def is_closed() -> bool:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            return call.room_closed_at is not None

    assert client.portal.call(is_closed)


def test_expired_incoming_call_cannot_be_accepted(client: TestClient) -> None:
    call_id, child_token, parent_token = create_call(client)
    container = client.app.state.container

    async def expire() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            call.started_at = datetime.now(UTC) - timedelta(minutes=2)
            await session.commit()

    client.portal.call(expire)
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 410
    client.portal.call(container.calls.maintain)
    result = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert result["state"] == "ANALYSIS_EXCLUDED"


def test_missing_upload_cannot_be_marked_complete(client: TestClient) -> None:
    call_id, _, parent_token = create_call(client)
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 200
    upload = client.post(
        f"/v1/calls/{call_id}/raw-audio/upload-url",
        headers=auth(parent_token),
        json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
    )
    response = client.post(
        f"/v1/calls/{call_id}/raw-audio/complete",
        headers=auth(parent_token),
        json={"assetId": upload.json()["assetId"]},
    )
    assert response.status_code == 409


async def test_maintenance_recovers_from_one_failure() -> None:
    recovered = asyncio.Event()
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database failure")
        recovered.set()

    task = asyncio.create_task(run_maintenance(operation, 0))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
