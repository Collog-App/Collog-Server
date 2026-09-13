from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import CallRecord
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family


def test_parent_can_call_child_and_only_parent_records_raw_audio(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    registered = client.post(
        "/v1/devices",
        headers=auth(child_token),
        json={"platform": "IOS", "token": "11" * 32, "voipToken": "22" * 32},
    )
    assert registered.status_code == 201
    response = client.post("/v1/calls", headers=auth(parent_token), json={"calleeId": child["id"]})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["callerId"] == parent["id"]
    assert created["calleeId"] == child["id"]
    assert created["rawCaptureRequired"] is True
    sent = client.app.state.container.voip_push.sent
    assert sent[-1][0] == "22" * 32
    assert sent[-1][1].caller_id == parent["id"]
    call_id = created["callId"]
    accepted = client.post(f"/v1/calls/{call_id}/accept", headers=auth(child_token))
    assert accepted.status_code == 200
    assert accepted.json()["rawCaptureRequired"] is False
    call = client.get(f"/v1/calls/{call_id}", headers=auth(parent_token)).json()
    assert call["parentId"] == parent["id"]
    assert call["childId"] == child["id"]
    raw = {"contentType": "audio/wav", "durationSec": 5, "sampleRate": 16000}
    assert (
        client.post(
            f"/v1/calls/{call_id}/raw-audio/upload-url", headers=auth(parent_token), json=raw
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/calls/{call_id}/raw-audio/upload-url", headers=auth(child_token), json=raw
        ).status_code
        == 403
    )
    assert client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token)).status_code == 200


def test_parent_outgoing_call_cannot_be_self_accepted_or_declined(client: TestClient) -> None:
    child_token, child, parent_token, _ = onboard_family(client)
    created = client.post(
        "/v1/calls", headers=auth(parent_token), json={"calleeId": child["id"]}
    ).json()
    path = f"/v1/calls/{created['callId']}"
    assert client.post(f"{path}/accept", headers=auth(parent_token)).status_code == 403
    assert client.post(f"{path}/decline", headers=auth(parent_token)).status_code == 403
    assert client.post(f"{path}/decline", headers=auth(child_token)).status_code == 200
    assert client.get(path, headers=auth(parent_token)).json()["state"] == "ANALYSIS_EXCLUDED"


def test_busy_participants_reject_new_calls_in_both_directions(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201
    for token, callee in ((child_token, parent["id"]), (parent_token, child["id"])):
        assert (
            client.post("/v1/calls", headers=auth(token), json={"calleeId": callee}).status_code
            == 409
        )
    assert (
        client.post(
            f"/v1/calls/{created.json()['callId']}/end", headers=auth(child_token)
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/calls", headers=auth(parent_token), json={"calleeId": child["id"]}
        ).status_code
        == 201
    )


def test_concurrent_call_creation_allows_only_one_room(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)

    def start(pair: tuple[str, str]):
        token, callee = pair
        return client.post("/v1/calls", headers=auth(token), json={"calleeId": callee})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(start, [(child_token, parent["id"]), (parent_token, child["id"])])
        )
    assert sorted(response.status_code for response in responses) == [201, 409]

    async def count_calls() -> int:
        async with client.app.state.container.database.sessions() as session:
            return await session.scalar(select(func.count(CallRecord.id)))

    assert client.portal.call(count_calls) == 1


def test_parent_cannot_call_unrelated_child(client: TestClient) -> None:
    _, _, parent_token, parent = onboard_family(client)
    _, stranger = create_user(client, "01099998888", "CHILD", "Stranger")
    for target in (stranger["id"], parent["id"]):
        assert (
            client.post(
                "/v1/calls", headers=auth(parent_token), json={"calleeId": target}
            ).status_code
            == 403
        )
