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
    def __init__(self, database: Database, gateway: ReportPushGateway) -> None:
        self.database = database
        self.gateway = gateway
        self._lock = asyncio.Lock()

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
                await session.commit()
                return
            devices = list(await session.scalars(
                select(Device)
                .where(
                    Device.user_id.in_([call.parent_id, call.child_id]),
                    Device.platform == "IOS",
                    Device.report_notifications_enabled.is_(True),
                    Device.push_token.is_not(None),
                    Device.push_token != "",
                )
            ))
            failed = False
            delivered: dict[str, str | None] = {}
            push = ReportReadyPush(call_id=call.id, expires_at=expires_at)
            for device in devices:
                token = device.push_token
                if token is None:
                    continue
                if token in delivered:
                    if delivered[token]:
                        device.apns_environment = delivered[token]
                    continue
                try:
                    environment = await self.gateway.send_report(
                        token, replace(push, apns_environment=device.apns_environment)
                    )
                    if environment:
                        device.apns_environment = environment
                    delivered[token] = environment
                except UnregisteredPushToken:
                    await session.execute(
                        update(Device).where(Device.push_token == token).values(push_token=None)
                    )
                except PushNotificationError:
                    failed = True
                    logger.warning("Report notification will retry", extra={"call_id": call.id})
            if not failed:
                call.report_notified_at = now
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
