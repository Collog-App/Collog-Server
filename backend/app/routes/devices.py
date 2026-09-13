from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from sqlalchemy import select, update

from app.container import AppContainer
from app.models import Device
from app.schemas import DeviceCreate
from app.security import CurrentUser, SessionDep
from app.services.notifications import (
    IncomingCallPush,
    PushNotificationError,
    UnregisteredVoipToken,
    VoipPushGateway,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def deliver_incoming_call_push(
    gateway: VoipPushGateway,
    device: Device,
    push: IncomingCallPush,
    container: AppContainer,
) -> bool:
    voip_token = device.voip_token
    if voip_token is None:
        return False
    try:
        environment = await gateway.send_incoming_call(voip_token, push)
        if environment:
            async with container.database.sessions() as session:
                await session.execute(
                    update(Device).where(Device.id == device.id, Device.voip_token == voip_token)
                    .values(apns_environment=environment)
                )
                await session.commit()
        return True
    except UnregisteredVoipToken as exc:
        async with container.database.sessions() as session:
            await session.execute(
                update(Device).where(Device.id == device.id, Device.voip_token == voip_token)
                .values(voip_token=None)
            )
            await session.commit()
        logger.warning("incoming VoIP token rejected for call %s: %s", push.call_id, exc)
    except PushNotificationError as exc:
        logger.warning("incoming VoIP push failed for call %s: %s", push.call_id, exc)
    return False


@router.post("/devices", status_code=201, tags=["Auth"])
async def create_device(
    payload: DeviceCreate, request: Request, user: CurrentUser, session: SessionDep
) -> dict:
    # Re-registration should be idempotent. A PushKit token belongs to an app
    # installation, so logging into another account transfers that installation.
    device = None
    if payload.voip_token:
        device = await session.scalar(
            select(Device).where(
                Device.platform == payload.platform,
                Device.voip_token == payload.voip_token,
            )
        )
    if device is None:
        device = await session.scalar(
            select(Device).where(
                Device.user_id == user.id,
                Device.platform == payload.platform,
                Device.token == payload.token,
            )
        )
    if device is None:
        device = Device(
            user_id=user.id,
            platform=payload.platform,
            token=payload.token,
            voip_token=payload.voip_token,
        )
        session.add(device)
    else:
        device.user_id = user.id
        device.token = payload.token
        device.voip_token = payload.voip_token
        device.created_at = datetime.now(UTC)
    device.call_notifications_enabled = payload.call_notifications_enabled
    if payload.apns_environment is not None:
        device.apns_environment = payload.apns_environment
    device.auth_session_id = request.state.auth_session_id
    device.push_token = payload.push_token
    device.report_notifications_enabled = payload.report_notifications_enabled
    await session.commit()
    return {"deviceId": device.id}
