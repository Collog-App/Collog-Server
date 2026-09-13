from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import func, or_, select, update

from app.auth_router import router as auth_router
from app.models import (
    AcousticAnalysisRun,
    AcousticFeature,
    AssetKind,
    AssetStatus,
    AudioAsset,
    Baseline,
    CallRecord,
    CallState,
    ChangeSignal,
    Device,
    ExtractionEvidence,
    HealthExtraction,
    ParentProfile,
    QuestionTtsGrant,
    RepeatEvent,
    Transcript,
    User,
    UserRole,
)
from app.routes.consents import router as consents_router
from app.routes.devices import deliver_incoming_call_push
from app.routes.devices import router as devices_router
from app.routes.families import router as families_router
from app.routes.profiles import router as profiles_router
from app.routes.shared import aware, call_to_dict, ensure_call_access, settings_from
from app.schemas import (
    AudioConstraints,
    CallAccepted,
    CallAcceptRequest,
    CallCreate,
    CallCreated,
    RawAudioComplete,
    RawAudioUploadRequest,
)
from app.security import CurrentUser, SessionDep
from app.services.domain import (
    ensure_child_can_access_parent,
    ensure_report_access,
    has_consent,
    latest_consent,
    participants_consented,
)
from app.services.livekit import LiveKitError
from app.services.notifications import IncomingCallPush
from app.services.questions import daily_questions
from app.services.repeat_detector import repeat_rate_per_minute
from app.services.signals import baseline_to_dict, signal_to_dict
from app.services.storage import LocalStorage
from app.services.tts import ElevenLabsDirectTtsGateway, QuestionTtsError

router = APIRouter()
router.include_router(auth_router)
router.include_router(devices_router)
router.include_router(families_router)
router.include_router(consents_router)
router.include_router(profiles_router)
logger = logging.getLogger(__name__)


async def recent_question_exclusions(session: SessionDep, parent_id: str) -> set[str]:
    calls = (
        await session.scalars(
            select(CallRecord)
            .where(CallRecord.parent_id == parent_id)
            .order_by(CallRecord.started_at.desc())
            .limit(3)
        )
    ).all()
    return {question_id for call in calls for question_id in call.asked_question_ids}


async def questions_for_parent(
    request: Request, session: SessionDep, parent_id: str, requester_id: str
):
    profile = await session.get(ParentProfile, parent_id)
    conditions = profile.conditions if profile else []
    excluded = await recent_question_exclusions(session, parent_id)
    source, questions = daily_questions(settings_from(request), conditions, excluded, parent_id)
    version = settings_from(request).consent_document_version
    if await has_consent(session, parent_id, version) and await has_consent(
        session, requester_id, version
    ):
        questions = await request.app.state.container.question_tts.attach_audio(questions)
    return source, questions


@router.get("/parents/{parentId}/daily-questions", tags=["Question"])
async def get_daily_questions(
    parent_id: Annotated[str, Path(alias="parentId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    await ensure_report_access(session, user, parent_id)
    source, questions = await questions_for_parent(request, session, parent_id, user.id)
    return {"source": source, "questions": [item.model_dump(by_alias=True) for item in questions]}


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


@router.post("/calls/{callId}/questions/{questionId}/tts-token", tags=["Question"])
async def create_question_tts_token(
    call_id: Annotated[str, Path(alias="callId")],
    question_id: Annotated[str, Path(alias="questionId", max_length=120)],
    request: Request,
    response: Response,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    container = request.app.state.container
    async with container.calls.reserve_participants([user.id]):
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())
        call = await session.scalar(
            select(CallRecord).where(CallRecord.id == call_id).with_for_update()
        )
        if call is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
        if call.effective_caller_id != user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "발신자만 질문 음성을 요청할 수 있습니다"
            )
        if not await participants_consented(
            session, call.parent_id, call.child_id, container.settings.consent_document_version
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "두 참여자의 AI 처리 동의가 필요해요")
        if call.state != CallState.RINGING.value or call.ended_at is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "수신 대기 중에만 질문 음성을 요청할 수 있습니다"
            )
        now = datetime.now(UTC)
        waiting_seconds = (now - aware(call.started_at)).total_seconds()
        if waiting_seconds >= container.settings.incoming_call_ttl_seconds:
            raise HTTPException(status.HTTP_410_GONE, "수신 대기 시간이 만료되었습니다")
        if question_id not in call.asked_question_ids:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "통화 질문을 찾을 수 없습니다")
        gateway = container.question_tts
        if (
            not container.settings.mock_external_services
            and not container.settings.elevenlabs_data_processing_approved
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "아이폰 기본 음성으로 재생해주세요")
        if not isinstance(gateway, ElevenLabsDirectTtsGateway):
            raise HTTPException(status.HTTP_409_CONFLICT, "직접 음성 재생이 설정되지 않았습니다")
        question_count = await session.scalar(
            select(func.count()).select_from(QuestionTtsGrant).where(
                QuestionTtsGrant.call_id == call_id, QuestionTtsGrant.question_id == question_id,
            )
        )
        user_count = await session.scalar(
            select(func.count()).select_from(QuestionTtsGrant).where(
                QuestionTtsGrant.user_id == user.id,
                QuestionTtsGrant.created_at > now - timedelta(hours=1),
            )
        )
        if question_count >= 2 or user_count >= 20:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "음성 요청 횟수를 초과했습니다")
        session.add(QuestionTtsGrant(user_id=user.id, call_id=call_id, question_id=question_id))
        await session.commit()
    try:
        token = await gateway.issue_token()
    except QuestionTtsError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return token.model_dump(by_alias=True)


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


@router.post("/calls/{callId}/raw-audio/upload-url", tags=["Call"])
async def raw_audio_upload_url(
    call_id: Annotated[str, Path(alias="callId")],
    payload: RawAudioUploadRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.scalar(
        select(CallRecord).where(CallRecord.id == call_id).with_for_update()
    )
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    if user.id != call.parent_id or not call.recording_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "원시 오디오 업로드 권한이 없습니다")
    if call.state not in {CallState.ACTIVE.value, CallState.ENDED.value}:
        raise HTTPException(status.HTTP_409_CONFLICT, "오디오 업로드 시간이 지났습니다")
    if call.ended_at and (datetime.now(UTC) - aware(call.ended_at)).total_seconds() > (
        settings_from(request).upload_url_ttl_seconds
    ):
        raise HTTPException(status.HTTP_410_GONE, "오디오 업로드 시간이 지났습니다")
    existing = await session.scalar(select(AudioAsset).where(
        AudioAsset.call_id == call.id,
        AudioAsset.kind == AssetKind.DEVICE_RAW.value,
        AudioAsset.status == AssetStatus.PENDING.value,
        AudioAsset.created_at >= datetime.now(UTC) - timedelta(
            seconds=settings_from(request).upload_url_ttl_seconds
        ),
    ).order_by(AudioAsset.created_at.desc()).limit(1))
    if existing is not None:
        upload_url = await request.app.state.container.storage.create_upload_url(
            request.app.state.container.storage.object_key(existing.uri), existing.content_type
        )
        return {
            "uploadUrl": upload_url,
            "assetId": existing.id,
            "expiresIn": max(0, settings_from(request).upload_url_ttl_seconds - int(
                (datetime.now(UTC) - aware(existing.created_at)).total_seconds()
            )),
        }
    key = f"calls/{call.id}/raw/parent-{random.randrange(10**10):010d}.wav"
    asset = AudioAsset(
        call_id=call.id,
        kind=AssetKind.DEVICE_RAW.value,
        uri=request.app.state.container.storage.object_uri(key),
        content_type=payload.content_type,
        duration_sec=payload.duration_sec,
        sample_rate=payload.sample_rate,
    )
    session.add(asset)
    await session.commit()
    upload_url = await request.app.state.container.storage.create_upload_url(
        key, payload.content_type
    )
    return {
        "uploadUrl": upload_url,
        "assetId": asset.id,
        "expiresIn": settings_from(request).upload_url_ttl_seconds,
    }


@router.put("/uploads/{encoded_key:path}", include_in_schema=False)
async def local_upload(
    encoded_key: str,
    request: Request,
    expires: int,
    signature: str,
) -> Response:
    storage = request.app.state.container.storage
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "로컬 업로드가 비활성화되어 있습니다")
    if not storage.verify_upload(encoded_key, expires, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "업로드 URL이 만료되었거나 잘못되었습니다")
    body = await request.body()
    await storage.write(encoded_key, body)
    return Response(status_code=204)


@router.get("/tts-assets/{encoded_key:path}", include_in_schema=False)
async def local_tts_asset(
    encoded_key: str,
    request: Request,
    expires: int,
    signature: str,
) -> Response:
    storage = request.app.state.container.storage
    key = encoded_key.lstrip("/")
    if not isinstance(storage, LocalStorage) or not key.startswith("tts/questions/"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "TTS 오디오를 찾을 수 없습니다")
    if not storage.verify_download(key, expires, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "TTS URL이 만료되었거나 잘못되었습니다")
    try:
        body = await storage.read(storage.object_uri(key))
    except Exception as exc:
        logger.warning("local TTS asset read failed: %s", exc)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "TTS 오디오를 찾을 수 없습니다") from exc
    return Response(
        content=body,
        media_type="audio/mpeg",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/calls/{callId}/raw-audio/complete", status_code=202, tags=["Call"])
async def raw_audio_complete(
    call_id: Annotated[str, Path(alias="callId")],
    payload: RawAudioComplete,
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.scalar(
        select(CallRecord).where(CallRecord.id == call_id).with_for_update()
    )
    asset = await session.get(AudioAsset, payload.asset_id)
    if call is None or asset is None or asset.call_id != call.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "오디오 자산을 찾을 수 없습니다")
    if user.id != call.parent_id or asset.kind != AssetKind.DEVICE_RAW.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "오디오 자산 접근 권한이 없습니다")
    if not call.recording_enabled:
        await session.rollback()
        await request.app.state.container.pipeline.purge_call_audio(call_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이번 통화의 녹음과 분석이 중단되었습니다")
    if asset.uploaded_at is not None and asset.status in {
        AssetStatus.UPLOADED.value,
        AssetStatus.PURGED.value,
    }:
        return {"status": "COMPLETED" if asset.status == AssetStatus.PURGED.value else "QUEUED"}
    if asset.status != AssetStatus.PENDING.value or call.state not in {
        CallState.ACTIVE.value,
        CallState.ENDED.value,
    }:
        raise HTTPException(status.HTTP_409_CONFLICT, "오디오 업로드 시간이 지났습니다")
    if (datetime.now(UTC) - aware(asset.created_at)).total_seconds() > settings_from(
        request
    ).upload_url_ttl_seconds:
        raise HTTPException(status.HTTP_410_GONE, "오디오 업로드 URL이 만료되었습니다")
    if not await request.app.state.container.storage.exists(asset.uri):
        raise HTTPException(status.HTTP_409_CONFLICT, "오디오 업로드가 완료되지 않았습니다")
    asset.status = AssetStatus.UPLOADED.value
    asset.uploaded_at = datetime.now(UTC)
    await session.commit()
    background.add_task(request.app.state.container.pipeline.process, call.id)
    return {"status": "QUEUED"}


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


@router.get("/calls/{callId}/transcript", tags=["Analysis"])
async def get_transcript(
    call_id: Annotated[str, Path(alias="callId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    await ensure_call_access(session, user, call)
    item = await session.scalar(select(Transcript).where(Transcript.call_id == call_id))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전사 결과가 아직 없습니다")
    repeat_events = list(
        await session.scalars(
            select(RepeatEvent).where(RepeatEvent.call_id == call_id).order_by(RepeatEvent.start_ms)
        )
    )
    return {
        "callId": call_id,
        "provider": item.provider,
        "excluded": item.excluded,
        "exclusionReason": item.exclusion_reason,
        "parentSpeechSec": item.parent_speech_sec,
        "segments": item.segments,
        "repeatEvents": [
            {
                "startMs": event.start_ms,
                "endMs": event.end_ms,
                "category": event.category,
                "matchedText": event.matched_text,
                "ruleId": event.rule_id,
                "confidence": event.confidence,
                "ruleVersion": event.rule_version,
            }
            for event in repeat_events
        ],
        "repeatRequestCount": len(repeat_events),
        "repeatRequestsPerMinute": repeat_rate_per_minute(
            len(repeat_events), item.parent_speech_sec
        ),
    }


@router.get("/calls/{callId}/extraction", tags=["Analysis"])
async def get_extraction(
    call_id: Annotated[str, Path(alias="callId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    await ensure_call_access(session, user, call)
    item = await session.scalar(select(HealthExtraction).where(HealthExtraction.call_id == call_id))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "추출 결과가 아직 없습니다")
    evidence = await session.scalar(
        select(ExtractionEvidence).where(ExtractionEvidence.call_id == call_id)
    )
    return {
        "callId": call_id,
        "parseStatus": item.parse_status,
        "symptom": item.symptom,
        "medication": item.medication,
        "activity": item.activity,
        "sleep": item.sleep,
        "facts": evidence.facts if evidence else [],
        "schemaVersion": evidence.schema_version if evidence else "v1",
        "rawTranscript": item.raw_transcript,
    }


@router.get("/calls/{callId}/acoustic-features", tags=["Analysis"])
async def get_acoustic_features(
    call_id: Annotated[str, Path(alias="callId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    await ensure_call_access(session, user, call)
    items = list(
        await session.scalars(select(AcousticFeature).where(AcousticFeature.call_id == call_id))
    )
    if not items:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "음향 분석 결과가 아직 없습니다")
    analysis_run = await session.scalar(
        select(AcousticAnalysisRun).where(AcousticAnalysisRun.call_id == call_id)
    )
    return {
        "callId": call_id,
        "audioSource": items[0].audio_source,
        "analyzerVersion": analysis_run.analyzer_version if analysis_run else None,
        "coughDetectorVersion": analysis_run.cough_detector_version if analysis_run else None,
        "features": [
            {
                "metric": item.metric,
                "value": item.value,
                "unit": item.unit,
                "status": item.status,
                "unmeasurableReason": item.unmeasurable_reason,
            }
            for item in items
        ],
    }


@router.get("/parents/{parentId}/baseline", tags=["Signal"])
async def get_baselines(
    parent_id: Annotated[str, Path(alias="parentId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    kind: Literal["ANCHOR", "ROLLING"] | None = None,
) -> dict:
    await ensure_report_access(session, user, parent_id)
    await request.app.state.container.signals.rebuild_baselines(session, parent_id)
    await session.commit()
    statement = select(Baseline).where(Baseline.parent_id == parent_id)
    if kind:
        statement = statement.where(Baseline.kind == kind)
    items = list(await session.scalars(statement.order_by(Baseline.metric, Baseline.time_slot)))
    return {"baselines": [baseline_to_dict(item) for item in items]}


@router.get("/parents/{parentId}/signals", tags=["Signal"])
async def get_signals(
    parent_id: Annotated[str, Path(alias="parentId")],
    user: CurrentUser,
    session: SessionDep,
    filter: Literal["ALL", "PROMOTED", "ACUTE"] = "ALL",
) -> dict:
    await ensure_report_access(session, user, parent_id)
    statement = select(ChangeSignal).where(ChangeSignal.parent_id == parent_id)
    if filter == "PROMOTED":
        statement = statement.where(ChangeSignal.promoted.is_(True))
    elif filter == "ACUTE":
        statement = statement.where(ChangeSignal.acute.is_(True))
    items = list(await session.scalars(statement.order_by(ChangeSignal.observed_at.desc())))
    return {"signals": [signal_to_dict(item) for item in items]}


@router.get("/parents/{parentId}/reports", tags=["Report"])
async def get_report(
    parent_id: Annotated[str, Path(alias="parentId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    period: Literal["WEEKLY", "MONTHLY"],
    date_: Annotated[date | None, Query(alias="date")] = None,
) -> dict:
    await ensure_report_access(session, user, parent_id)
    return await request.app.state.container.reports.get_or_issue(session, parent_id, period, date_)


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


@router.get("/health", include_in_schema=False)
async def health(request: Request, session: SessionDep) -> dict:
    await session.execute(select(1))
    tasks = getattr(request.app.state, "maintenance_tasks", [])
    if any(task.done() for task in tasks):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "정기 작업이 중단되었습니다")
    return {"status": "ok"}
