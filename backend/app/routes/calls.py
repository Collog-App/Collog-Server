from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Query, Request, status
from sqlalchemy import or_, select, update

from app.models import AssetKind, AudioAsset, CallRecord, CallState, Device, User, UserRole
from app.routes.devices import deliver_incoming_call_push
from app.routes.questions import questions_for_parent
from app.routes.shared import aware, call_to_dict, ensure_call_access, settings_from
from app.schemas import AudioConstraints, CallAccepted, CallAcceptRequest, CallCreate, CallCreated
from app.security import CurrentUser, SessionDep
from app.services.domain import (
    ensure_child_can_access_parent,
    ensure_report_access,
    latest_consent,
    participants_consented,
)
from app.services.livekit import LiveKitError
from app.services.notifications import IncomingCallPush

router = APIRouter()


@router.post("/calls", status_code=201, tags=["Call"])
async def create_call(
    payload: CallCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    callee = await session.get(User, payload.callee_id)
    if callee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "가족 계정을 찾을 수 없습니다")
    if user.id == callee.id or user.role == callee.role:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "가족의 부모와 자녀 사이에 통화할 수 있습니다"
        )
    parent, child = (user, callee) if user.role == UserRole.PARENT.value else (callee, user)
    await ensure_child_can_access_parent(session, child, parent.id)
    async with request.app.state.container.calls.reserve_participants([parent.id, child.id]):
        return await create_reserved_call(request, user, callee, parent, child, session)


async def create_reserved_call(
    request: Request,
    user: User,
    callee: User,
    parent: User,
    child: User,
    session: SessionDep,
) -> dict:
    participants = [parent.id, child.id]
    locked = list(await session.scalars(
        select(User.id).where(User.id.in_(participants)).order_by(User.id).with_for_update()
    ))
    if len(locked) != 2:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "가족 계정을 찾을 수 없습니다")
    await session.refresh(parent)
    await session.refresh(child)
    if parent.role != UserRole.PARENT.value or child.role != UserRole.CHILD.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "사용자 역할이 변경되었습니다. 다시 시도해주세요"
        )
    await ensure_child_can_access_parent(session, child, parent.id)
    busy = await session.scalar(
        select(CallRecord.id)
        .where(
            or_(CallRecord.parent_id.in_(participants), CallRecord.child_id.in_(participants)),
            CallRecord.ended_at.is_(None),
            CallRecord.state.in_(
                [CallState.CREATED.value, CallState.RINGING.value, CallState.ACTIVE.value]
            ),
        )
        .limit(1)
    )
    if busy is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "본인 또는 상대방이 이미 통화 중입니다")
    if not settings_from(request).mock_external_services:
        receiver = await session.scalar(
            select(Device.id).where(
                Device.user_id == callee.id,
                Device.platform == "IOS",
                Device.voip_token.is_not(None),
                Device.call_notifications_enabled.is_(True),
            )
        )
        if receiver is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "상대방의 통화 수신 기기가 등록되지 않았습니다"
            )
    source, questions = await questions_for_parent(request, session, parent.id, child.id)
    del source
    recording_enabled = await participants_consented(
        session, parent.id, child.id, settings_from(request).consent_document_version
    )
    latest = await latest_consent(session, parent.id)
    disabled_reason = None
    if not recording_enabled:
        disabled_reason = "CONSENT_DENIED" if latest else "CONSENT_PENDING"
    if (
        not settings_from(request).mock_external_services
        and not settings_from(request).gemini_data_processing_approved
    ):
        recording_enabled = False
        disabled_reason = "PROVIDER_PRIVACY_PENDING"
    call = CallRecord(
        parent_id=parent.id,
        child_id=child.id,
        caller_id=user.id,
        state=CallState.RINGING.value,
        room_name=f"collog-{datetime.now(UTC):%Y%m%d}-{random.randrange(10**10):010d}",
        recording_enabled=recording_enabled,
        recording_disabled_reason=disabled_reason,
        asked_question_ids=[item.question_id for item in questions],
    )
    session.add(call)
    await session.flush()
    livekit = request.app.state.container.livekit
    try:
        await livekit.create_room(call.room_name)
    except LiveKitError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    token = livekit.participant_token(call.room_name, user.id, user.name)
    devices = list(await session.scalars(
        select(Device)
        .where(
            Device.user_id == callee.id,
            Device.platform == "IOS",
            Device.voip_token.is_not(None),
            Device.call_notifications_enabled.is_(True),
        )
        .order_by(Device.created_at.desc())
    ))
    await session.commit()
    delivered = False
    for device in devices:
        device_delivered = await deliver_incoming_call_push(
            request.app.state.container.voip_push,
            device,
            IncomingCallPush(
                call_id=call.id,
                caller_id=user.id,
                caller_name=user.name,
                callee_id=callee.id,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings_from(request).incoming_call_ttl_seconds),
                apns_environment=device.apns_environment,
            ),
            request.app.state.container,
        )
        delivered = delivered or device_delivered
    if not delivered and (devices or not settings_from(request).mock_external_services):
        await request.app.state.container.calls.finish(call.id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "상대방에게 통화 알림을 보내지 못했어요. 상대방 앱에서 다시 로그인한 뒤 시도해주세요",
        )
    response = CallCreated(
        call_id=call.id,
        caller_id=call.effective_caller_id,
        callee_id=call.callee_id,
        raw_capture_required=recording_enabled and user.id == parent.id,
        livekit_url=settings_from(request).livekit_url,
        room_name=call.room_name,
        access_token=token,
        recording_enabled=recording_enabled,
        recording_disabled_reason=disabled_reason,
        recording_disabled_message="녹음과 AI 분석 없이 통화해요" if disabled_reason else None,
        questions=questions,
        audio_constraints=AudioConstraints(),
    )
    return response.model_dump(by_alias=True)


@router.post("/calls/{callId}/accept", tags=["Call"])
async def accept_call(
    call_id: Annotated[str, Path(alias="callId")],
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    session: SessionDep,
    payload: CallAcceptRequest | None = None,
) -> dict:
    call = await session.scalar(
        select(CallRecord).where(CallRecord.id == call_id).with_for_update()
    )
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    if call.callee_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "수신 권한이 없습니다")
    request_id = str(payload.request_id) if payload else None
    if (
        call.state == CallState.ACTIVE.value and call.ended_at is None
        and request_id is not None and call.accepted_request_id == request_id
    ):
        livekit = request.app.state.container.livekit
        return CallAccepted(
            call_id=call.id,
            recording_enabled=call.recording_enabled,
            livekit_url=settings_from(request).livekit_url,
            room_name=call.room_name,
            access_token=livekit.participant_token(call.room_name, user.id, user.name),
            raw_capture_required=call.recording_enabled and user.id == call.parent_id,
            audio_constraints=AudioConstraints(),
        ).model_dump(by_alias=True)
    if call.state not in {CallState.RINGING.value, CallState.CREATED.value}:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 종료되었거나 응답한 통화입니다")
    settings = settings_from(request)
    if (
        datetime.now(UTC) - aware(call.started_at)
    ).total_seconds() >= settings.incoming_call_ttl_seconds:
        raise HTTPException(status.HTTP_410_GONE, "수신 대기 시간이 만료되었습니다")
    if call.recording_enabled and not await participants_consented(
        session, call.parent_id, call.child_id, settings.consent_document_version
    ):
        call.recording_enabled = False
        call.recording_disabled_reason = "CONSENT_DENIED"
    if not settings.mock_external_services and not settings.gemini_data_processing_approved:
        call.recording_enabled = False
        call.recording_disabled_reason = "PROVIDER_PRIVACY_PENDING"
    accepted_at = datetime.now(UTC)
    claimed = await session.execute(
        update(CallRecord).where(
            CallRecord.id == call_id,
            CallRecord.state.in_([CallState.CREATED.value, CallState.RINGING.value]),
            CallRecord.ended_at.is_(None),
        ).values(
            state=CallState.ACTIVE.value, accepted_at=accepted_at, accepted_request_id=request_id
        )
    )
    if claimed.rowcount != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 종료되었거나 응답한 통화입니다")
    settings = settings_from(request)
    livekit = request.app.state.container.livekit
    token = livekit.participant_token(call.room_name, user.id, user.name)
    if call.recording_enabled and not settings.allow_raw_only_analysis:
        for _, kind, filename in (
            (call.parent_id, AssetKind.WEBRTC_EGRESS_PARENT, "parent.ogg"),
            (call.child_id, AssetKind.WEBRTC_EGRESS_CHILD, "child.ogg"),
        ):
            key = f"calls/{call.id}/egress/{filename}"
            asset = AudioAsset(
                call_id=call.id,
                kind=kind.value,
                uri=request.app.state.container.storage.object_uri(key),
                content_type="audio/ogg",
            )
            session.add(asset)
    await session.commit()
    if call.recording_enabled and not settings.allow_raw_only_analysis:
        background.add_task(request.app.state.container.calls.start_recordings, call.id)
    response = CallAccepted(
        call_id=call.id,
        recording_enabled=call.recording_enabled,
        livekit_url=settings.livekit_url,
        room_name=call.room_name,
        access_token=token,
        raw_capture_required=call.recording_enabled and user.id == call.parent_id,
        audio_constraints=AudioConstraints(),
    )
    return response.model_dump(by_alias=True)


@router.post("/calls/{callId}/decline", tags=["Call"])
async def decline_call(
    call_id: Annotated[str, Path(alias="callId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    await ensure_call_access(session, user, call)
    if call.state not in {CallState.RINGING.value, CallState.CREATED.value}:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 처리된 통화입니다")
    if user.id != call.callee_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "수신자만 거절할 수 있습니다")
    declined = await session.execute(
        update(CallRecord).where(
            CallRecord.id == call_id,
            CallRecord.state.in_([CallState.CREATED.value, CallState.RINGING.value]),
            CallRecord.ended_at.is_(None),
        ).values(
            state=CallState.ANALYSIS_EXCLUDED.value,
            ended_at=datetime.now(UTC),
            duration_sec=0,
            recording_enabled=False,
            recording_disabled_reason="CALL_NOT_ANSWERED",
        )
    )
    if declined.rowcount != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 처리된 통화입니다")
    await session.commit()
    await request.app.state.container.calls.finish(call_id)
    return {"status": "DECLINED"}


@router.post("/calls/{callId}/end", tags=["Call"])
async def end_call(
    call_id: Annotated[str, Path(alias="callId")],
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    await ensure_call_access(session, user, call)
    await session.rollback()
    await request.app.state.container.calls.finish(call_id)
    call = await session.get(CallRecord, call_id, populate_existing=True)
    background.add_task(request.app.state.container.pipeline.process, call_id)
    return call_to_dict(call)


@router.get("/calls/{callId}", tags=["Call"])
async def get_call(
    call_id: Annotated[str, Path(alias="callId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    await ensure_call_access(session, user, call)
    return call_to_dict(call)


@router.get("/parents/{parentId}/calls", tags=["Call"])
async def list_calls(
    parent_id: Annotated[str, Path(alias="parentId")],
    user: CurrentUser,
    session: SessionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
) -> dict:
    await ensure_report_access(session, user, parent_id)
    statement = select(CallRecord).where(CallRecord.parent_id == parent_id)
    if from_:
        statement = statement.where(
            CallRecord.started_at >= datetime.combine(from_, datetime.min.time())
        )
    if to:
        statement = statement.where(
            CallRecord.started_at < datetime.combine(to + timedelta(days=1), datetime.min.time())
        )
    calls = list(await session.scalars(statement.order_by(CallRecord.started_at.desc())))
    return {"calls": [call_to_dict(call) for call in calls]}
