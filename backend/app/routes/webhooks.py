from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from app.models import AssetStatus, AudioAsset, CallRecord, CallState
from app.security import SessionDep
from app.services.billing import apply_revenuecat_event
from app.services.livekit import LiveKitError
from app.services.revenuecat import RevenueCatError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/livekit", status_code=204, tags=["Webhook"])
async def livekit_webhook(
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    authorization: Annotated[str, Header()] = "",
) -> Response:
    body = (await request.body()).decode()
    try:
        event = request.app.state.container.livekit.receive_webhook(body, authorization)
    except LiveKitError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    event_name = event.get("event")
    if event_name == "room_finished":
        room_name = (event.get("room") or {}).get("name")
        call = await session.scalar(select(CallRecord).where(CallRecord.room_name == room_name))
        if call is not None:
            call_id = call.id
            await session.rollback()
            await request.app.state.container.calls.finish(call_id)
            background.add_task(request.app.state.container.pipeline.process, call_id)
        return Response(status_code=204)
    if event_name == "track_published":
        room = event.get("room") or {}
        participant = event.get("participant") or {}
        track = event.get("track") or {}
        room_name = room.get("name")
        identity = participant.get("identity")
        track_id = track.get("sid")
        track_type = track.get("type")
        if not room_name or not identity or not track_id:
            return Response(status_code=204)
        if track_type not in (None, "AUDIO", "0", 0):
            return Response(status_code=204)
        call = await session.scalar(select(CallRecord).where(CallRecord.room_name == room_name))
        if call is None or not call.recording_enabled or call.state != CallState.ACTIVE.value:
            return Response(status_code=204)
        if identity not in {call.parent_id, call.child_id}:
            return Response(status_code=204)
        background.add_task(
            request.app.state.container.calls.start_recordings, call.id, {identity: track_id}
        )
        return Response(status_code=204)
    if event_name != "egress_ended":
        return Response(status_code=204)
    info = event.get("egress_info") or event.get("egressInfo") or {}
    egress_id = info.get("egress_id") or info.get("egressId")
    if not egress_id:
        return Response(status_code=204)
    asset = await session.scalar(select(AudioAsset).where(AudioAsset.egress_id == egress_id))
    if asset is None:
        return Response(status_code=204)
    call = await session.scalar(
        select(CallRecord).where(CallRecord.id == asset.call_id).with_for_update()
    )
    if call is None:
        return Response(status_code=204)
    if call.state == CallState.ACTIVE.value and call.recording_enabled:
        call.recording_enabled = False
        call.recording_disabled_reason = "RECORDING_INTERRUPTED"
    if not call.recording_enabled:
        asset.status = AssetStatus.UPLOADED.value
        asset.uploaded_at = datetime.now(UTC)
        await session.commit()
        background.add_task(request.app.state.container.calls.stop_recordings, call.id)
        background.add_task(request.app.state.container.pipeline.purge_call_audio, call.id)
        return Response(status_code=204)
    if asset.status in {AssetStatus.UPLOADED.value, AssetStatus.PURGED.value}:
        return Response(status_code=204)
    egress_status = str(info.get("status", "EGRESS_COMPLETE"))
    if egress_status in {"EGRESS_COMPLETE", "3", "COMPLETE"}:
        asset.status = AssetStatus.UPLOADED.value
        asset.uploaded_at = datetime.now(UTC)
    else:
        asset.status = AssetStatus.FAILED.value
    await session.commit()
    background.add_task(request.app.state.container.pipeline.process, asset.call_id)
    return Response(status_code=204)


@router.post("/webhooks/revenuecat", tags=["Webhook"])
async def revenuecat_webhook(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str, Header()] = "",
) -> dict:
    """구독 이벤트. RevenueCat 은 200 이 아니면 최대 5번 다시 보낸다.

    REST 조회까지 이 요청 안에서 끝낸다. 실패하면 503 을 돌려 RevenueCat 이 다시 보내게 하고,
    처리한 이벤트 표(revenuecat_events)에는 남기지 않는다. RevenueCat 은 60초 안에 답이 없으면
    실패로 보므로 그보다 짧게 끊는다.
    """
    container = request.app.state.container
    expected = container.settings.revenuecat_webhook_auth
    if not expected.strip():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "결제 웹훅이 설정되지 않았습니다")
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "웹훅 인증에 실패했습니다")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "JSON 본문이 아닙니다") from exc
    event = body.get("event") if isinstance(body, dict) else None
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "event.id 가 없습니다")
    try:
        async with asyncio.timeout(40):
            result = await apply_revenuecat_event(
                session, container.settings, container.revenuecat, event, datetime.now(UTC)
            )
    except (RevenueCatError, TimeoutError) as exc:
        await session.rollback()
        logger.warning("RevenueCat 웹훅 반영 실패, 재시도를 기다립니다: %s", type(exc).__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "구독 정보를 확인하지 못했습니다"
        ) from exc
    return {"status": result}


@router.get("/health", include_in_schema=False)
async def health(request: Request, session: SessionDep) -> dict:
    await session.execute(select(1))
    tasks = getattr(request.app.state, "maintenance_tasks", [])
    if any(task.done() for task in tasks):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "정기 작업이 중단되었습니다")
    return {"status": "ok"}
