from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from app.config import Settings
from app.database import Database
from app.models import AssetKind, AssetStatus, AudioAsset, CallRecord, CallState, TimeSlot
from app.services.livekit import LiveKitError, LiveKitGateway
from app.services.storage import StorageGateway

logger = logging.getLogger(__name__)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class CallLifecycle:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        livekit: LiveKitGateway,
        storage: StorageGateway,
    ) -> None:
        self.settings = settings
        self.database = database
        self.livekit = livekit
        self.storage = storage
        self._participant_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @asynccontextmanager
    async def reserve_participants(self, user_ids: list[str]) -> AsyncIterator[None]:
        locks = [
            self._participant_locks.setdefault(user_id, asyncio.Lock())
            for user_id in sorted(set(user_ids))
        ]
        async with AsyncExitStack() as stack:
            for lock in locks:
                await stack.enter_async_context(lock)
            yield

    async def start_recordings(self, call_id: str, tracks: dict[str, str] | None = None) -> None:
        if self.settings.allow_raw_only_analysis:
            return
        async with self.database.sessions() as session:
            call = await session.scalar(
                select(CallRecord).where(CallRecord.id == call_id).with_for_update()
            )
            if call is None or call.state != CallState.ACTIVE.value or not call.recording_enabled:
                return
            assets = list(
                await session.scalars(
                    select(AudioAsset).where(
                        AudioAsset.call_id == call_id,
                        AudioAsset.kind != AssetKind.DEVICE_RAW.value,
                        AudioAsset.egress_id.is_(None),
                        AudioAsset.status == AssetStatus.PENDING.value,
                    )
                )
            )
            for asset in assets:
                identity = (
                    call.parent_id
                    if asset.kind == AssetKind.WEBRTC_EGRESS_PARENT.value
                    else call.child_id
                )
                try:
                    track_id = (tracks or {}).get(identity)
                    if track_id is None:
                        track_id = await self.livekit.find_audio_track_id(call.room_name, identity)
                    if track_id:
                        started = await self.livekit.start_track_egress(
                            call.room_name, track_id, self.storage.object_key(asset.uri)
                        )
                        asset.egress_id = started.egress_id
                except LiveKitError:
                    logger.exception("Recording startup will retry", extra={"call_id": call_id})
            await session.commit()

    async def finish(self, call_id: str) -> None:
        async with self.database.sessions() as session:
            call = await session.scalar(
                select(CallRecord).where(CallRecord.id == call_id).with_for_update()
            )
            if call is None:
                return
            if call.ended_at is None:
                call.ended_at = datetime.now(UTC)
                call.state = CallState.ENDED.value
                started = aware(call.accepted_at or call.started_at)
                call.duration_sec = (
                    max(0, round((call.ended_at - started).total_seconds()))
                    if call.accepted_at
                    else 0
                )
                hour = started.astimezone(ZoneInfo("Asia/Seoul")).hour
                call.time_slot = (
                    TimeSlot.MORNING.value if 6 <= hour <= 11 else TimeSlot.AFTERNOON_EVENING.value
                )
                assets = await session.scalars(
                    select(AudioAsset).where(
                        AudioAsset.call_id == call.id,
                        AudioAsset.kind != AssetKind.DEVICE_RAW.value,
                        AudioAsset.egress_id.is_(None),
                        AudioAsset.status == AssetStatus.PENDING.value,
                    )
                )
                for asset in assets:
                    asset.status = AssetStatus.FAILED.value
                await session.commit()
            if call.room_closed_at is not None:
                return
            room_name = call.room_name

        try:
            await self.livekit.delete_room(room_name)
        except LiveKitError:
            logger.exception("Call room cleanup will retry", extra={"call_id": call_id})
            return
        async with self.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            if call is not None:
                call.room_closed_at = datetime.now(UTC)
                await session.commit()

    async def maintain(self) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            calls = list(
                await session.scalars(
                    select(CallRecord)
                    .where(
                        CallRecord.room_closed_at.is_(None),
                        or_(
                            CallRecord.ended_at.is_not(None),
                            CallRecord.started_at <= now - timedelta(hours=2),
                            and_(
                                CallRecord.state.in_(
                                    [CallState.CREATED.value, CallState.RINGING.value]
                                ),
                                CallRecord.started_at
                                <= now - timedelta(seconds=self.settings.incoming_call_ttl_seconds),
                            ),
                        ),
                    )
                    .order_by(CallRecord.started_at)
                    .limit(100)
                )
            )
        for call in calls:
            ringing_expired = (
                call.state in {CallState.CREATED.value, CallState.RINGING.value}
                and (now - aware(call.started_at)).total_seconds()
                >= self.settings.incoming_call_ttl_seconds
            )
            duration_expired = now - aware(call.started_at) >= timedelta(hours=2)
            if call.ended_at is not None or ringing_expired or duration_expired:
                await self.finish(call.id)
        async with self.database.sessions() as session:
            recording_ids = list(
                await session.scalars(
                    select(CallRecord.id)
                    .join(AudioAsset, AudioAsset.call_id == CallRecord.id)
                    .where(
                        CallRecord.state == CallState.ACTIVE.value,
                        AudioAsset.kind != AssetKind.DEVICE_RAW.value,
                        AudioAsset.egress_id.is_(None),
                        AudioAsset.status == AssetStatus.PENDING.value,
                    )
                    .distinct()
                    .limit(100)
                )
            )
        for call_id in recording_ids:
            await self.start_recordings(call_id)
