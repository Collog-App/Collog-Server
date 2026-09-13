from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Settings
from app.database import Database
from app.models import CallRecord, CallState, Device, User
from app.services.notifications import (
    ApnsReportPushGateway,
    MockReportPushGateway,
    PushNotificationError,
    ReportReadyPush,
    UnregisteredPushToken,
    create_report_push_gateway,
)
from app.services.report_notifications import ReportNotifications


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}")
    await database.ensure_schema(auto_reset=True)
    yield database
    await database.close()


async def seed_call(database: Database, *, age_days: int = 0) -> tuple[str, str, str]:
    async with database.sessions() as session:
        parent = User(phone="01012345678", name="Parent", role="PARENT")
        child = User(phone="01087654321", name="Child", role="CHILD")
        session.add_all([parent, child])
        await session.flush()
        call = CallRecord(
            parent_id=parent.id,
            child_id=child.id,
            room_name="report-test",
            state=CallState.ANALYZED.value,
            ended_at=datetime.now(UTC) - timedelta(days=age_days),
        )
        session.add(call)
        await session.commit()
        return call.id, parent.id, child.id


@pytest.mark.asyncio
async def test_report_targets_and_preferences_are_separate_from_voip(database: Database) -> None:
    call_id, parent_id, child_id = await seed_call(database)
    async with database.sessions() as session:
        session.add_all([
            Device(user_id=parent_id, platform="IOS", token="parent", push_token="a" * 64,
                   call_notifications_enabled=False),
            Device(user_id=child_id, platform="IOS", token="child", push_token="b" * 64),
            Device(user_id=child_id, platform="IOS", token="duplicate", push_token="b" * 64),
            Device(user_id=child_id, platform="IOS", token="off", push_token="c" * 64,
                   report_notifications_enabled=False),
            Device(user_id=parent_id, platform="IOS", token="voip-only", voip_token="d" * 64),
        ])
        await session.commit()
    gateway = MockReportPushGateway()
    notifications = ReportNotifications(database, gateway)
    await asyncio.gather(notifications.notify(call_id), notifications.notify(call_id))
    await notifications.maintain()
    assert {token for token, _ in gateway.sent} == {"a" * 64, "b" * 64}
    assert len(gateway.sent) == 2
    async with database.sessions() as session:
        call = await session.get(CallRecord, call_id)
        assert call is not None and call.report_notified_at is not None
        assert call.state == CallState.ANALYZED.value


@pytest.mark.asyncio
async def test_report_delivery_failure_retries_without_changing_analysis(
    database: Database,
) -> None:
    call_id, parent_id, _ = await seed_call(database)
    async with database.sessions() as session:
        session.add(Device(user_id=parent_id, platform="IOS", token="parent", push_token="a" * 64))
        await session.commit()

    class FailingGateway(MockReportPushGateway):
        async def send_report(self, token: str, push: ReportReadyPush) -> None:
            raise PushNotificationError("Provider unavailable")

    await ReportNotifications(database, FailingGateway()).maintain()
    async with database.sessions() as session:
        call = await session.get(CallRecord, call_id)
        assert call is not None and call.report_notified_at is None
        assert call.state == CallState.ANALYZED.value
    gateway = MockReportPushGateway()
    await ReportNotifications(database, gateway).maintain()
    assert len(gateway.sent) == 1


@pytest.mark.asyncio
async def test_unregistered_push_token_is_cleared(database: Database) -> None:
    call_id, parent_id, _ = await seed_call(database)
    async with database.sessions() as session:
        device = Device(user_id=parent_id, platform="IOS", token="parent", push_token="a" * 64,
                        voip_token="b" * 64)
        session.add(device)
        await session.commit()
        device_id = device.id

    class GoneGateway(MockReportPushGateway):
        async def send_report(self, token: str, push: ReportReadyPush) -> None:
            raise UnregisteredPushToken("Unregistered")

    await ReportNotifications(database, GoneGateway()).maintain()
    async with database.sessions() as session:
        device = await session.get(Device, device_id)
        call = await session.get(CallRecord, call_id)
        assert device is not None and device.push_token is None
        assert device.voip_token == "b" * 64
        assert call is not None and call.report_notified_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("age_days", [0, 2])
async def test_no_recipients_and_expired_calls_are_completed(
    database: Database, age_days: int
) -> None:
    call_id, parent_id, _ = await seed_call(database, age_days=age_days)
    if age_days:
        async with database.sessions() as session:
            session.add(Device(user_id=parent_id, platform="IOS", token="x", push_token="a" * 64))
            await session.commit()
    gateway = MockReportPushGateway()
    await ReportNotifications(database, gateway).maintain()
    assert not gateway.sent
    async with database.sessions() as session:
        call = await session.get(CallRecord, call_id)
        assert call is not None and call.report_notified_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("response_status", [200, 410, 503])
async def test_apns_alert_uses_regular_topic_and_private_payload(response_status: int) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    push = ReportReadyPush(call_id="report-call", expires_at=datetime.now(UTC) + timedelta(days=1))

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["apns-topic"] == "com.test.app"
        assert request.headers["apns-push-type"] == "alert"
        assert request.headers["apns-collapse-id"] == push.call_id
        assert int(request.headers["apns-expiration"]) == int(push.expires_at.timestamp())
        body = json.loads(request.content)
        assert set(body) == {"aps", "report"}
        assert body["report"] == {"callId": push.call_id}
        assert body["aps"]["alert"]["body"] == "새 통화 기록을 확인해 주세요."
        return httpx.Response(response_status)

    settings = Settings(_env_file=None, apns_team_id="TEAM", apns_key_id="KEY",
                        apns_bundle_id="com.test.app")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = ApnsReportPushGateway(settings, client=client, private_key=pem)
        if response_status == 410:
            with pytest.raises(UnregisteredPushToken):
                await gateway.send_report("a" * 64, push)
        elif response_status != 200:
            with pytest.raises(PushNotificationError):
                await gateway.send_report("a" * 64, push)
        else:
            await gateway.send_report("a" * 64, push)


@pytest.mark.asyncio
async def test_missing_real_apns_configuration_never_succeeds() -> None:
    settings = Settings(_env_file=None, app_env="development", mock_external_services=True,
                        apns_private_key_path=None)
    gateway = create_report_push_gateway(settings)
    with pytest.raises(PushNotificationError):
        await gateway.send_report("a" * 64, ReportReadyPush("call", datetime.now(UTC)))
