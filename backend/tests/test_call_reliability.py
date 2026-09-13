from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AudioAsset
from tests.conftest import auth
from tests.test_api_flow import onboard_family
from tests.test_call_recovery import create_call


def test_accept_and_decline_have_one_winner(client: TestClient) -> None:
    call_id, child_token, parent_token = create_call(client)
    path = f"/v1/calls/{call_id}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda action: client.post(f"{path}/{action}", headers=auth(parent_token)),
            ["accept", "decline"],
        ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    state = client.get(path, headers=auth(child_token)).json()["state"]
    assert state == ("ACTIVE" if responses[0].status_code == 200 else "ANALYSIS_EXCLUDED")


def test_accept_retry_keeps_owner_and_assets(client: TestClient) -> None:
    call_id, _, parent_token = create_call(client)
    path = f"/v1/calls/{call_id}/accept"
    payload = {"requestId": str(uuid4())}
    for _ in range(2):
        assert client.post(path, headers=auth(parent_token), json=payload).status_code == 200
    assert client.post(path, headers=auth(parent_token), json={
        "requestId": str(uuid4()),
    }).status_code == 409

    async def assets() -> list[AudioAsset]:
        async with client.app.state.container.database.sessions() as session:
            return list(await session.scalars(select(AudioAsset)))

    assert len(client.portal.call(assets)) == 2


def test_all_registered_devices_receive_call(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    for index in [1, 2]:
        assert client.post("/v1/devices", headers=auth(parent_token), json={
            "platform": "IOS", "token": f"device-{index}", "voipToken": str(index) * 64,
        }).status_code == 201
    assert client.post("/v1/calls", headers=auth(child_token), json={
        "calleeId": parent["id"],
    }).status_code == 201
    sent = client.app.state.container.voip_push.sent
    assert {token for token, _ in sent} == {"1" * 64, "2" * 64}
    assert all(push.payload()["call"]["calleeId"] == parent["id"] for _, push in sent)


def test_replaced_track_disables_analysis_without_ending_call(client: TestClient) -> None:
    call_id, child_token, parent_token = create_call(client)
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 200
    call = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    client.portal.call(client.app.state.container.calls.start_recordings, call_id, {
        call["parentId"]: "replacement-track",
    })
    updated = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert updated["state"] == "ACTIVE"
    assert updated["recordingEnabled"] is False
    assert updated["recordingDisabledReason"] == "RECORDING_INTERRUPTED"


def test_raw_upload_retry_reuses_asset(client: TestClient) -> None:
    call_id, _, parent_token = create_call(client)
    client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    responses = [client.post(
        f"/v1/calls/{call_id}/raw-audio/upload-url", headers=auth(parent_token),
        json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
    ) for _ in range(2)]
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()["assetId"] == responses[1].json()["assetId"]


def test_late_raw_completion_is_rejected_after_recording_stops(client: TestClient) -> None:
    call_id, child_token, parent_token = create_call(client)
    client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    upload = client.post(
        f"/v1/calls/{call_id}/raw-audio/upload-url", headers=auth(parent_token),
        json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
    ).json()
    call = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    client.portal.call(client.app.state.container.calls.start_recordings, call_id, {
        call["parentId"]: "replacement-track",
    })
    assert client.put(upload["uploadUrl"], content=b"late audio").status_code == 204
    response = client.post(
        f"/v1/calls/{call_id}/raw-audio/complete", headers=auth(parent_token),
        json={"assetId": upload["assetId"]},
    )
    assert response.status_code == 403

    async def removed() -> bool:
        container = client.app.state.container
        async with container.database.sessions() as session:
            asset = await session.get(AudioAsset, upload["assetId"])
            return not await container.storage.exists(asset.uri)

    assert client.portal.call(removed)


def test_absent_peer_gets_grace_before_cleanup(client: TestClient) -> None:
    call_id, child_token, parent_token = create_call(client)
    client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    container = client.app.state.container

    async def missing(room_name: str) -> set[str]:
        return set()

    container.livekit.participant_identities = missing
    client.portal.call(container.calls.maintain)
    path = f"/v1/calls/{call_id}"
    assert client.get(path, headers=auth(child_token)).json()["state"] == "ACTIVE"
    container.calls._missing_peer_since[call_id] = datetime.now(UTC) - timedelta(seconds=61)
    client.portal.call(container.calls.maintain)
    assert client.get(path, headers=auth(child_token)).json()["endedAt"] is not None
