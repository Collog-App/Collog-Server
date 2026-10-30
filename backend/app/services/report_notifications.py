from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.database import Database
from app.models import CallRecord, CallState, Device
from app.services.notifications import (
    PushNotificationError,
    ReportPushGateway,
    ReportReadyPush,
    UnregisteredPushToken,
)

logger = logging.getLogger(__name__)


class ReportNotifications:
    def __init__(
        self,
        database: Database,
        gateway: ReportPushGateway,
        android_gateway: ReportPushGateway | None = None,
    ) -> None:
        self.database = database
        self.gateway = gateway
        self.android_gateway = android_gateway
        self._lock = asyncio.Lock()
        # 재시도 때 이미 받은 기기에 다시 보내지 않도록 통화별로 보낸 (플랫폼, 토큰)을 기억한다.
        # 프로세스가 다시 뜨면 비지만, 실패한 기기 하나 때문에 다른 기기가
        # 10초마다 받는 일은 막는다.
        self._delivered: dict[str, dict[tuple[str, str], str | None]] = {}

    async def notify(self, call_id: str) -> None:
        async with self._lock, self.database.sessions() as session:
            call = await session.scalar(
                select(CallRecord)
                .where(
                    CallRecord.id == call_id,
                    CallRecord.state == CallState.ANALYZED.value,
                    CallRecord.report_notified_at.is_(None),
                )
                .with_for_update(skip_locked=True)
            )
            if call is None:
                return
            now = datetime.now(UTC)
            ended_at = call.ended_at or call.started_at
            expires_at = ended_at.replace(tzinfo=UTC) + timedelta(days=1)
            if expires_at <= now:
                call.report_notified_at = now
                self._delivered.pop(call.id, None)
                await session.commit()
                return
            devices = list(await session.scalars(
                select(Device)
                .where(
                    Device.user_id.in_([call.parent_id, call.child_id]),
                    Device.platform.in_(
                        ["IOS", "ANDROID"] if self.android_gateway is not None else ["IOS"]
                    ),
                    Device.report_notifications_enabled.is_(True),
                    Device.push_token.is_not(None),
                    Device.push_token != "",
                )
            ))
            failed = False
            delivered = self._delivered.setdefault(call.id, {})
            push = ReportReadyPush(call_id=call.id, expires_at=expires_at)
            for device in devices:
                token = device.push_token
                if token is None:
                    continue
                key = (device.platform, token)
                if key in delivered:
                    if delivered[key]:
                        device.apns_environment = delivered[key]
                    continue
                try:
                    gateway = (
                        self.android_gateway
                        if device.platform == "ANDROID" and self.android_gateway is not None
                        else self.gateway
                    )
                    environment = await gateway.send_report(
                        token, replace(push, apns_environment=device.apns_environment)
                    )
                    if environment:
                        device.apns_environment = environment
                    delivered[key] = environment
                except UnregisteredPushToken:
                    # 이 기기 행만 지운다. 같은 문자열을 등록한
                    # 다른 사용자·플랫폼 행은 건드리지 않는다.
                    await session.execute(
                        update(Device)
                        .where(Device.id == device.id, Device.push_token == token)
                        .values(push_token=None)
                    )
                except PushNotificationError:
                    failed = True
                    logger.warning("Report notification will retry", extra={"call_id": call.id})
            if not failed:
                call.report_notified_at = now
                self._delivered.pop(call.id, None)
            await session.commit()

    async def maintain(self) -> None:
        async with self.database.sessions() as session:
            call_ids = list(await session.scalars(
                select(CallRecord.id)
                .where(
                    CallRecord.state == CallState.ANALYZED.value,
                    CallRecord.report_notified_at.is_(None),
                )
                .order_by(CallRecord.ended_at, CallRecord.started_at)
                .limit(100)
            ))
        for call_id in call_ids:
            await self.notify(call_id)
