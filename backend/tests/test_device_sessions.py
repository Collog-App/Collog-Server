from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Device
from tests.conftest import auth
from tests.test_auth import login


def device_tokens(client: TestClient, device_id: str) -> tuple[str | None, str | None, str | None]:
    async def read() -> tuple[str | None, str | None, str | None]:
        async with client.app.state.container.database.sessions() as session:
            device = await session.get(Device, device_id)
            assert device is not None
            return device.voip_token, device.push_token, device.auth_session_id

    assert client.portal is not None
    return client.portal.call(read)


def test_logout_clears_only_its_registered_device_tokens(client: TestClient) -> None:
    first = login(client)
    second = login(client)
    device_ids: list[str] = []
    for index, tokens in enumerate((first, second)):
        response = client.post(
            "/v1/devices",
            headers=auth(tokens["accessToken"]),
            json={
                "platform": "IOS",
                "token": f"installation-{index}",
                "voipToken": str(index + 1) * 64,
                "pushToken": str(index + 3) * 64,
            },
        )
        assert response.status_code == 201
        device_ids.append(response.json()["deviceId"])
    first_before = device_tokens(client, device_ids[0])
    second_before = device_tokens(client, device_ids[1])
    assert first_before[:2] == ("1" * 64, "3" * 64)
    assert second_before[:2] == ("2" * 64, "4" * 64)
    assert first_before[2] is not None and first_before[2] != second_before[2]
    response = client.post("/v1/auth/logout", json={"refreshToken": first["refreshToken"]})
    assert response.status_code == 204
    assert device_tokens(client, device_ids[0])[:2] == (None, None)
    assert device_tokens(client, device_ids[1]) == second_before


def test_old_session_logout_preserves_re_registered_installation(client: TestClient) -> None:
    first = login(client)
    second = login(client)
    payload = {
        "platform": "IOS", "token": "installation", "voipToken": "a" * 64,
        "pushToken": "b" * 64,
    }
    first_device = client.post("/v1/devices", headers=auth(first["accessToken"]), json=payload)
    second_device = client.post("/v1/devices", headers=auth(second["accessToken"]), json=payload)
    assert first_device.status_code == second_device.status_code == 201
    assert first_device.json()["deviceId"] == second_device.json()["deviceId"]
    device_id = second_device.json()["deviceId"]
    before = device_tokens(client, device_id)
    assert client.post(
        "/v1/auth/logout", json={"refreshToken": first["refreshToken"]}
    ).status_code == 204
    assert device_tokens(client, device_id) == before
    assert client.post(
        "/v1/auth/logout", json={"refreshToken": second["refreshToken"]}
    ).status_code == 204
    assert device_tokens(client, device_id)[:2] == (None, None)
