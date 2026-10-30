from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.models import Device
from app.services.fcm import (
    FCM_SCOPE,
    FcmClient,
    FcmReportPushGateway,
    FcmVoipPushGateway,
    create_android_push_gateways,
)
from app.services.notifications import (
    DisabledVoipPushGateway,
    IncomingCallPush,
    MockReportPushGateway,
    PushNotificationError,
    ReportPushGateway,
    ReportReadyPush,
    UnregisteredPushToken,
    UnregisteredVoipToken,
    VoipPushGateway,
)
from app.services.report_notifications import ReportNotifications
from tests.conftest import auth
from tests.test_api_flow import onboard_family
from tests.test_report_notifications import seed_call


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def credentials(key: rsa.RSAPrivateKey) -> dict:
    return {
        "project_id": "collog-test",
        "client_email": "fcm@collog-test.iam.gserviceaccount.com",
        "private_key_id": "key-1",
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "token_uri": "https://oauth2.googleapis.com/token",
    }


class FakeGoogle:
    def __init__(
        self, key: rsa.RSAPrivateKey, send_status: int = 200, send_body: dict | None = None
    ):
        self.key = key
        self.send_status = send_status
        self.send_body = send_body or {"name": "projects/collog-test/messages/1"}
        self.token_requests = 0
        self.messages: list[dict] = []
        self.authorizations: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_requests += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            claims = jwt.decode(
                form["assertion"][0],
                self.key.public_key(),
                algorithms=["RS256"],
                audience="https://oauth2.googleapis.com/token",
                leeway=timedelta(hours=2),
            )
            assert claims["iss"] == "fcm@collog-test.iam.gserviceaccount.com"
            assert claims["scope"] == FCM_SCOPE
            assert jwt.get_unverified_header(form["assertion"][0])["kid"] == "key-1"
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3599})
        assert str(request.url) == "https://fcm.googleapis.com/v1/projects/collog-test/messages:send"
        self.authorizations.append(request.headers["authorization"])
        self.messages.append(json.loads(request.content))
        return httpx.Response(self.send_status, json=self.send_body)


def push(expires_in: int = 30) -> IncomingCallPush:
    return IncomingCallPush(
        call_id="call-1",
        caller_id="child",
        caller_name="자녀",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        callee_id="parent",
    )


async def test_incoming_call_is_a_high_priority_data_message(key) -> None:
    google = FakeGoogle(key)
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(Settings(_env_file=None), client=http, credentials=credentials(key))
        gateway = FcmVoipPushGateway(client)
        for _ in range(2):
            assert await gateway.send_incoming_call("android-token", push()) is None
    assert google.token_requests == 1
    assert google.authorizations == ["Bearer access-1"] * 2
    message = google.messages[0]["message"]
    assert message["token"] == "android-token"
    assert message["android"]["priority"] == "HIGH"
    assert message["android"]["ttl"] in {"29s", "30s"}
    assert "notification" not in message
    assert message["data"] == {
        "type": "incoming_call",
        "callId": "call-1",
        "callUUID": "call-1",
        "callerId": "child",
        "calleeId": "parent",
        "callerName": "자녀",
        "expiresAt": message["data"]["expiresAt"],
    }
    assert all(isinstance(value, str) for value in message["data"].values())


async def test_access_token_is_renewed_before_expiry(key) -> None:
    google = FakeGoogle(key)
    now = [time.time()]
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(
            Settings(_env_file=None),
            client=http,
            credentials=credentials(key),
            clock=lambda: now[0],
        )
        await client.access_token()
        now[0] += 3_500
        await client.access_token()
        now[0] += 100
        await client.access_token()
    assert google.token_requests == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"error": {"status": "NOT_FOUND", "details": [{
            "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
            "errorCode": "UNREGISTERED",
        }]}}),
        (400, {"error": {"status": "INVALID_ARGUMENT", "details": [
            {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
             "errorCode": "INVALID_ARGUMENT"},
            {"@type": "type.googleapis.com/google.rpc.BadRequest",
             "fieldViolations": [{"field": "message.token", "description": "invalid"}]},
        ]}}),
    ],
)
async def test_dead_tokens_are_reported_as_unregistered(key, status, body) -> None:
    google = FakeGoogle(key, send_status=status, send_body=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(Settings(_env_file=None), client=http, credentials=credentials(key))
        with pytest.raises(UnregisteredVoipToken):
            await FcmVoipPushGateway(client).send_incoming_call("gone", push())
        with pytest.raises(UnregisteredPushToken):
            await FcmReportPushGateway(client).send_report(
                "gone", ReportReadyPush("call-1", datetime.now(UTC) + timedelta(days=1))
            )


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_transient_failures_keep_the_token(key, status) -> None:
    google = FakeGoogle(key, send_status=status, send_body={"error": {"status": "UNAVAILABLE"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(Settings(_env_file=None), client=http, credentials=credentials(key))
        with pytest.raises(PushNotificationError) as raised:
            await FcmVoipPushGateway(client).send_incoming_call("token", push())
        assert not isinstance(raised.value, UnregisteredVoipToken)


async def test_report_message_carries_no_health_data(key) -> None:
    google = FakeGoogle(key)
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(Settings(_env_file=None), client=http, credentials=credentials(key))
        await FcmReportPushGateway(client).send_report(
            "token", ReportReadyPush("call-1", datetime.now(UTC) + timedelta(days=1))
        )
    assert google.messages[0]["message"]["data"] == {
        "type": "report_ready",
        "callId": "call-1",
        "title": "Collog",
        "body": "새 통화 기록을 확인해 주세요.",
    }


def test_missing_service_account_disables_android_push(tmp_path: Path, key) -> None:
    for unset in (None, "", "  "):
        settings = Settings(
            _env_file=None, mock_external_services=False, fcm_service_account_path=unset
        )
        assert settings.fcm_service_account_path is None
        voip, report = create_android_push_gateways(settings)
        assert isinstance(voip, DisabledVoipPushGateway)
        assert report is None
    broken = tmp_path / "broken.json"
    broken.write_text("{}")
    with pytest.raises(PushNotificationError):
        create_android_push_gateways(
            Settings(_env_file=None, mock_external_services=False, fcm_service_account_path=broken)
        )


def test_android_callee_gets_the_fcm_push(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    response = client.post("/v1/devices", headers=auth(parent_token), json={
        "platform": "ANDROID", "token": "fcm-token",
        "voipToken": "fcm-token", "pushToken": "fcm-token",
    })
    assert response.status_code == 201, response.text

    class Recorder(VoipPushGateway):
        def __init__(self) -> None:
            self.sent: list[tuple[str, IncomingCallPush]] = []

        async def send_incoming_call(self, token: str, push: IncomingCallPush) -> None:
            self.sent.append((token, push))

    class Unused(VoipPushGateway):
        async def send_incoming_call(self, token: str, push: IncomingCallPush) -> None:
            pytest.fail("Android 기기에 APNs로 보내면 안 된다")

    container = client.app.state.container
    container.android_voip_push = Recorder()
    container.voip_push = Unused()
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201, created.text
    assert [token for token, _ in container.android_voip_push.sent] == ["fcm-token"]
    assert container.android_voip_push.sent[0][1].callee_id == parent["id"]


async def test_report_routes_android_devices_to_fcm(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'android-reports.db'}")
    await database.ensure_schema(auto_reset=True)
    try:
        call_id, parent_id, child_id = await seed_call(database)
        async with database.sessions() as session:
            session.add_all([
                Device(user_id=parent_id, platform="IOS", token="ios", push_token="a" * 64),
                Device(
                    user_id=child_id, platform="ANDROID", token="android", push_token="fcm-token"
                ),
            ])
            await session.commit()

        ios: ReportPushGateway = MockReportPushGateway()
        android = MockReportPushGateway()
        await ReportNotifications(database, ios, android).notify(call_id)
        assert [token for token, _ in ios.sent] == ["a" * 64]
        assert [token for token, _ in android.sent] == ["fcm-token"]
    finally:
        await database.close()


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"status": "INVALID_ARGUMENT"}},
        {"error": {"status": "INVALID_ARGUMENT", "details": [
            {"@type": "type.googleapis.com/google.rpc.BadRequest",
             "fieldViolations": [{"field": "message.data", "description": "too big"}]},
        ]}},
    ],
)
async def test_other_invalid_arguments_keep_the_token(key, body) -> None:
    google = FakeGoogle(key, send_status=400, send_body=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(Settings(_env_file=None), client=http, credentials=credentials(key))
        with pytest.raises(PushNotificationError) as raised:
            await FcmVoipPushGateway(client).send_incoming_call("token", push())
        assert not isinstance(raised.value, UnregisteredVoipToken)


async def test_unauthorized_send_drops_the_cached_access_token(key) -> None:
    google = FakeGoogle(key, send_status=401, send_body={"error": {"status": "UNAUTHENTICATED"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(Settings(_env_file=None), client=http, credentials=credentials(key))
        for _ in range(2):
            with pytest.raises(PushNotificationError):
                await client.send("token", {"type": "x"}, 30)
    assert google.token_requests == 2


def test_unexpected_token_uri_is_rejected(key) -> None:
    with pytest.raises(PushNotificationError):
        FcmClient(
            Settings(_env_file=None),
            credentials={**credentials(key), "token_uri": "https://attacker.example/token"},
        )


async def test_report_messages_collapse_per_call(key) -> None:
    google = FakeGoogle(key)
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as http:
        client = FcmClient(Settings(_env_file=None), client=http, credentials=credentials(key))
        await FcmReportPushGateway(client).send_report(
            "token", ReportReadyPush("call-1", datetime.now(UTC) + timedelta(days=1))
        )
    assert google.messages[0]["message"]["android"]["collapse_key"] == "call-1"


def test_android_only_callee_passes_the_receiver_check(client: TestClient, tmp_path: Path) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    client.post("/v1/devices", headers=auth(parent_token), json={
        "platform": "ANDROID", "token": "fcm-token",
        "voipToken": "fcm-token", "pushToken": "fcm-token",
    })

    class Recorder(VoipPushGateway):
        sent: list[str] = []

        async def send_incoming_call(self, token: str, push: IncomingCallPush) -> None:
            self.sent.append(token)

    container = client.app.state.container
    container.settings.mock_external_services = False
    container.settings.fcm_service_account_path = tmp_path / "fcm.json"
    container.android_voip_push = Recorder()
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201, created.text
    assert Recorder.sent == ["fcm-token"]


async def test_failed_android_report_does_not_resend_to_ios(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'resend.db'}")
    await database.ensure_schema(auto_reset=True)
    try:
        call_id, parent_id, child_id = await seed_call(database)
        async with database.sessions() as session:
            session.add_all([
                Device(user_id=parent_id, platform="IOS", token="ios", push_token="a" * 64),
                Device(
                    user_id=child_id, platform="ANDROID", token="android", push_token="fcm-token"
                ),
            ])
            await session.commit()

        class FailingAndroid(MockReportPushGateway):
            async def send_report(self, token: str, push: ReportReadyPush) -> None:
                raise PushNotificationError("FCM 403")

        ios = MockReportPushGateway()
        notifications = ReportNotifications(database, ios, FailingAndroid())
        for _ in range(3):
            await notifications.maintain()
        assert [token for token, _ in ios.sent] == ["a" * 64]
    finally:
        await database.close()


async def test_dead_android_token_only_clears_its_own_row(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'scoped.db'}")
    await database.ensure_schema(auto_reset=True)
    try:
        call_id, parent_id, child_id = await seed_call(database)
        shared = "a" * 64
        async with database.sessions() as session:
            ios = Device(user_id=parent_id, platform="IOS", token="ios", push_token=shared)
            android = Device(
                user_id=child_id, platform="ANDROID", token="android", push_token=shared
            )
            session.add_all([ios, android])
            await session.commit()
            ios_id, android_id = ios.id, android.id

        class GoneAndroid(MockReportPushGateway):
            async def send_report(self, token: str, push: ReportReadyPush) -> None:
                raise UnregisteredPushToken("INVALID_ARGUMENT")

        await ReportNotifications(database, MockReportPushGateway(), GoneAndroid()).notify(call_id)
        async with database.sessions() as session:
            assert (await session.get(Device, ios_id)).push_token == shared
            assert (await session.get(Device, android_id)).push_token is None
    finally:
        await database.close()
