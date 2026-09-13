from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.models import CallRecord, Device
from app.services.notifications import (
    ApnsVoipPushGateway,
    IncomingCallPush,
    PushNotificationError,
    UnregisteredVoipToken,
    VoipPushGateway,
)
from tests.conftest import auth
from tests.test_api_flow import onboard_family


@pytest.mark.parametrize("environment", [None, "sandbox", "production"])
async def test_apns_recovers_wrong_environment(environment: str | None) -> None:
    hosts = []

    def respond(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api.sandbox.push.apple.com":
            return httpx.Response(400, json={"reason": "BadDeviceToken"})
        return httpx.Response(200)

    key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    settings = Settings(_env_file=None, apns_environment="sandbox", apns_team_id="TEAM",
                        apns_key_id="KEY", apns_bundle_id="com.test.app")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = ApnsVoipPushGateway(settings, client=client, private_key=key)
        push = IncomingCallPush("call", "caller", "Caller", datetime.now(UTC), environment)
        assert await gateway.send_incoming_call("a" * 64, push) == "production"
    expected = ["api.push.apple.com"] if environment == "production" else [
        "api.sandbox.push.apple.com", "api.push.apple.com",
    ]
    assert hosts == expected


@pytest.mark.parametrize("status,reason", [(410, "Unregistered"), (403, "InvalidProviderToken"),
                                          (503, "ServiceUnavailable")])
async def test_apns_does_not_retry_other_failures(status: int, reason: str) -> None:
    hosts = []

    def respond(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(status, json={"reason": reason})

    key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    settings = Settings(_env_file=None, apns_environment="sandbox", apns_team_id="TEAM",
                        apns_key_id="KEY", apns_bundle_id="com.test.app")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = ApnsVoipPushGateway(settings, client=client, private_key=key)
        with pytest.raises(PushNotificationError):
            await gateway.send_incoming_call(
                "a" * 64, IncomingCallPush("call", "caller", "Caller", datetime.now(UTC))
            )
    assert len(hosts) == 1


def test_expired_latest_device_preserves_reachable_call(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    for index in [1, 2]:
        response = client.post("/v1/devices", headers=auth(parent_token), json={
            "platform": "IOS", "token": f"device-{index}", "voipToken": str(index) * 64,
        })
        assert response.status_code == 201

    class Gateway(VoipPushGateway):
        async def send_incoming_call(self, token: str, push: IncomingCallPush) -> str | None:
            if token == "2" * 64:
                raise UnregisteredVoipToken("Unregistered")
            return "production"

    container = client.app.state.container
    container.voip_push = Gateway()
    response = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert response.status_code == 201, response.text

    async def check() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, response.json()["callId"])
            assert call.ended_at is None
            devices = list(await session.scalars(select(Device).order_by(Device.token)))
            assert devices[0].apns_environment == "production"
            assert devices[1].voip_token is None

    client.portal.call(check)


def test_failed_push_returns_error_before_room_credentials(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    client.post("/v1/devices", headers=auth(parent_token), json={
        "platform": "IOS", "token": "device", "voipToken": "a" * 64,
    })

    class Gateway(VoipPushGateway):
        async def send_incoming_call(self, token: str, push: IncomingCallPush) -> str | None:
            raise PushNotificationError("Unavailable")

    container = client.app.state.container
    container.voip_push = Gateway()
    response = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert response.status_code == 502
    assert "accessToken" not in response.json()

    async def check() -> None:
        async with container.database.sessions() as session:
            call = await session.scalar(select(CallRecord))
            assert call.ended_at is not None
            assert call.room_closed_at is not None

    client.portal.call(check)
