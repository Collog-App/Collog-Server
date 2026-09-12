from __future__ import annotations

import logging
import random
import string
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
from sqlalchemy import select, update

from app.auth_router import router as auth_router
from app.config import Settings
from app.container import AppContainer
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
    ConsentDecision,
    ConsentRecord,
    Device,
    ExtractionEvidence,
    Family,
    FamilyMember,
    HealthExtraction,
    Invitation,
    ParentProfile,
    RepeatEvent,
    Transcript,
    User,
    UserRole,
)
from app.schemas import (
    AudioConstraints,
    CallAccepted,
    CallCreate,
    CallCreated,
    ConsentSubmit,
    DeviceCreate,
    InvitationAccept,
    InvitationCreate,
    ProfilePut,
    RawAudioComplete,
    RawAudioUploadRequest,
)
from app.security import CurrentUser, SessionDep, require_role
from app.services.domain import (
    derived_member_status,
    ensure_child_can_access_parent,
    ensure_report_access,
    family_for_child,
    has_consent,
    latest_consent,
    latest_invitation,
)
from app.services.livekit import LiveKitError
from app.services.notifications import (
    IncomingCallPush,
    PushNotificationError,
    UnregisteredVoipToken,
    VoipPushGateway,
)
from app.services.questions import daily_questions
from app.services.repeat_detector import repeat_rate_per_minute
from app.services.signals import baseline_to_dict, signal_to_dict
from app.services.storage import LocalStorage

router = APIRouter()
router.include_router(auth_router)
logger = logging.getLogger(__name__)

CONSENT_ITEMS = [
    "SENSITIVE_HEALTH_COLLECTION",
    "VOICE_FEATURE_EXTRACTION",
    "CALL_RECORDING",
    "REPORT_SHARING_WITH_CHILD",
]


def settings_from(request: Request) -> Settings:
    return request.app.state.container.settings


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def call_to_dict(call: CallRecord) -> dict:
    return {
        "callId": call.id,
        "parentId": call.parent_id,
        "childId": call.child_id,
        "state": call.state,
        "timeSlot": call.time_slot,
        "startedAt": call.started_at,
        "endedAt": call.ended_at,
        "durationSec": call.duration_sec,
        "recorded": call.recording_enabled,
        "parentSpeechSec": call.parent_speech_sec,
        "askedQuestionIds": call.asked_question_ids,
        "rawAudioPurgedAt": call.raw_audio_purged_at,
    }


async def ensure_call_access(session: SessionDep, user: User, call: CallRecord) -> None:
    if user.id not in {call.parent_id, call.child_id}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "통화 접근 권한이 없습니다")


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


async def questions_for_parent(request: Request, session: SessionDep, parent_id: str):
    profile = await session.get(ParentProfile, parent_id)
    conditions = profile.conditions if profile else []
    excluded = await recent_question_exclusions(session, parent_id)
    source, questions = daily_questions(settings_from(request), conditions, excluded, parent_id)
    questions = await request.app.state.container.question_tts.attach_audio(questions)
    return source, questions


async def deliver_incoming_call_push(
    gateway: VoipPushGateway,
    voip_token: str,
    push: IncomingCallPush,
    container: AppContainer,
) -> None:
    try:
        await gateway.send_incoming_call(voip_token, push)
    except UnregisteredVoipToken:
        async with container.database.sessions() as session:
            await session.execute(
                update(Device).where(Device.voip_token == voip_token).values(voip_token=None)
            )
            await session.commit()
        await container.calls.finish(push.call_id)
    except PushNotificationError as exc:
        logger.warning("incoming VoIP push failed for call %s: %s", push.call_id, exc)
        await container.calls.finish(push.call_id)


@router.post("/devices", status_code=201, tags=["Auth"])
async def create_device(payload: DeviceCreate, user: CurrentUser, session: SessionDep) -> dict:
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
    await session.commit()
    return {"deviceId": device.id}


# Family
@router.post("/families/{familyId}/invitations", status_code=201, tags=["Family"])
async def create_invitation(
    family_id: Annotated[str, Path(alias="familyId")],
    payload: InvitationCreate,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    require_role(user, UserRole.CHILD)
    family = await family_for_child(session, user.id)
    if family is None or family.id != family_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")
    member = FamilyMember(
        family_id=family.id,
        name=payload.name,
        relation=payload.relation,
        invited_at=datetime.now(UTC),
    )
    session.add(member)
    await session.flush()
    code = await unique_invitation_code(session)
    invitation = Invitation(
        member_id=member.id,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(invitation)
    await session.commit()
    return invitation_dict(invitation)


async def unique_invitation_code(session: SessionDep) -> str:
    for _ in range(20):
        code = "".join(random.choices(string.digits, k=6))
        if await session.scalar(select(Invitation.id).where(Invitation.code == code)) is None:
            return code
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "초대 코드를 만들지 못했습니다")


def invitation_dict(invitation: Invitation) -> dict:
    status_value = (
        "ACCEPTED"
        if invitation.accepted_at
        else ("EXPIRED" if aware(invitation.expires_at) <= datetime.now(UTC) else "PENDING")
    )
    return {
        "invitationId": invitation.id,
        "code": invitation.code,
        "shareText": f"콜록 가족 초대 코드 {invitation.code}를 앱에 입력해주세요.",
        "expiresAt": invitation.expires_at,
        "status": status_value,
    }


@router.get("/families/{familyId}/members", tags=["Family"])
async def get_members(
    family_id: Annotated[str, Path(alias="familyId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    # 구성원 목록은 그 가족에 속한 사람이면 볼 수 있다. 자녀는 가족을 만든 사람으로,
    # 부모는 초대를 수락한 구성원으로 확인한다.
    family = await session.get(Family, family_id)
    membership = await session.scalar(
        select(FamilyMember.id).where(
            FamilyMember.family_id == family_id,
            FamilyMember.user_id == user.id,
        )
    )
    if family is None or (family.created_by != user.id and membership is None):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")
    members = list(
        await session.scalars(select(FamilyMember).where(FamilyMember.family_id == family_id))
    )
    output = []
    for member in members:
        invitation = await latest_invitation(session, member.id)
        member_status = await derived_member_status(session, member, invitation)
        output.append(
            {
                "memberId": member.id,
                "userId": member.user_id,
                "name": member.name,
                "relation": member.relation,
                "status": member_status,
                "canRegisterConditions": member_status == "CONSENT_GRANTED",
                "invitedAt": member.invited_at,
                "expiresAt": invitation.expires_at if invitation else None,
            }
        )
    return {"members": output}


@router.post("/invitations/{invitationId}/resend", status_code=201, tags=["Family"])
async def resend_invitation(
    invitation_id: Annotated[str, Path(alias="invitationId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    require_role(user, UserRole.CHILD)
    old = await session.get(Invitation, invitation_id)
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "초대를 찾을 수 없습니다")
    member = await session.get(FamilyMember, old.member_id)
    family = await family_for_child(session, user.id)
    if member is None or family is None or member.family_id != family.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")
    invitation = Invitation(
        member_id=member.id,
        code=await unique_invitation_code(session),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(invitation)
    await session.commit()
    return invitation_dict(invitation)


@router.post("/invitations/accept", tags=["Family"])
async def accept_invitation(
    payload: InvitationAccept, user: CurrentUser, session: SessionDep
) -> dict:
    require_role(user, UserRole.PARENT)
    invitation = await session.scalar(
        select(Invitation)
        .where(Invitation.code == payload.code)
        .order_by(Invitation.created_at.desc())
    )
    if invitation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "초대 코드를 찾을 수 없습니다")
    if aware(invitation.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, "만료된 초대예요. 다시 초대를 요청해주세요")
    member = await session.get(FamilyMember, invitation.member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "가족 구성원을 찾을 수 없습니다")
    if member.user_id and member.user_id != user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 다른 계정이 수락한 초대입니다")
    member.user_id = user.id
    invitation.accepted_at = datetime.now(UTC)
    await session.commit()
    return {"familyId": member.family_id, "memberId": member.id, "status": "AWAITING_CONSENT"}


# Consent
@router.get("/consents/document", tags=["Consent"])
async def consent_document(request: Request) -> dict:
    version = settings_from(request).consent_document_version
    return {
        "version": version,
        "fullText": (
            "통화 중 증상·복약·활동·수면 및 음성 특징값을 수집해 개인의 과거 기록과 "
            "비교합니다. 원본 오디오는 분석 직후 즉시 폐기하며 특징값과 구조화 텍스트만 "
            "저장합니다. 결과는 의료 진단이나 치료 지시가 아닙니다."
        ),
        "collectedItems": ["증상", "복약", "활동", "수면", "음성 특징값"],
        "purpose": "가족 통화 기반 건강 변화 기록과 리포트 제공",
        "retentionPeriod": "동의 철회 시까지",
        "rawAudioPolicy": "원본 오디오는 분석 직후 즉시 폐기합니다",
        "requiredItems": CONSENT_ITEMS,
    }


@router.post("/consents", status_code=201, tags=["Consent"])
async def submit_consent(
    payload: ConsentSubmit, request: Request, user: CurrentUser, session: SessionDep
) -> dict:
    require_role(user, UserRole.PARENT)
    settings = settings_from(request)
    if payload.document_version != settings.consent_document_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "최신 동의 안내를 다시 확인해주세요")
    if not payload.scrolled_to_end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "안내 내용을 끝까지 확인해주세요")
    if payload.decision == "GRANT" and not set(CONSENT_ITEMS).issubset(payload.agreed_items):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "필수 항목에 모두 동의해야 시작할 수 있어요"
        )
    record = ConsentRecord(
        user_id=user.id,
        document_version=payload.document_version,
        decision=(
            ConsentDecision.GRANTED.value
            if payload.decision == "GRANT"
            else ConsentDecision.DENIED.value
        ),
        agreed_items=payload.agreed_items if payload.decision == "GRANT" else [],
        agreed_at=datetime.now(UTC),
    )
    session.add(record)
    await session.commit()
    return consent_dict(record)


def consent_dict(record: ConsentRecord) -> dict:
    return {
        "consentId": record.id,
        "userId": record.user_id,
        "documentVersion": record.document_version,
        "status": record.decision,
        "agreedItems": record.agreed_items,
        "agreedAt": record.agreed_at,
    }


@router.get("/consents/me", tags=["Consent"])
async def my_consent(user: CurrentUser, session: SessionDep) -> dict:
    record = await latest_consent(session, user.id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "동의 기록이 없습니다")
    return consent_dict(record)


# Profile and questions
@router.get("/parents/{parentId}/profile", tags=["Profile"])
async def get_profile(
    parent_id: Annotated[str, Path(alias="parentId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    await ensure_report_access(session, user, parent_id)
    profile = await session.get(ParentProfile, parent_id)
    if profile is None:
        return {"parentId": parent_id, "conditions": [], "updatedAt": None}
    return {
        "parentId": profile.parent_id,
        "conditions": profile.conditions,
        "updatedAt": profile.updated_at,
    }


@router.put("/parents/{parentId}/profile", tags=["Profile"])
async def put_profile(
    parent_id: Annotated[str, Path(alias="parentId")],
    payload: ProfilePut,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    if user.role == UserRole.CHILD.value:
        await ensure_child_can_access_parent(session, user, parent_id)
    elif user.id != parent_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "프로필 접근 권한이 없습니다")
    if not await has_consent(session, parent_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "부모님 동의가 완료되어야 등록할 수 있어요")
    profile = await session.get(ParentProfile, parent_id)
    if profile is None:
        profile = ParentProfile(parent_id=parent_id, conditions=payload.conditions)
        session.add(profile)
    else:
        profile.conditions = payload.conditions
        profile.updated_at = datetime.now(UTC)
    await session.commit()
    return {
        "parentId": profile.parent_id,
        "conditions": profile.conditions,
        "updatedAt": profile.updated_at,
    }


@router.get("/parents/{parentId}/daily-questions", tags=["Question"])
async def get_daily_questions(
    parent_id: Annotated[str, Path(alias="parentId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    await ensure_report_access(session, user, parent_id)
    source, questions = await questions_for_parent(request, session, parent_id)
    return {"source": source, "questions": [item.model_dump(by_alias=True) for item in questions]}


# Call
@router.post("/calls", status_code=201, tags=["Call"])
async def create_call(
    payload: CallCreate,
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    require_role(user, UserRole.CHILD)
    await ensure_child_can_access_parent(session, user, payload.callee_id)
    parent = await session.get(User, payload.callee_id)
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "부모 계정을 찾을 수 없습니다")
    if settings_from(request).app_env == "production":
        receiver = await session.scalar(
            select(Device.id).where(
                Device.user_id == parent.id,
                Device.platform == "IOS",
                Device.voip_token.is_not(None),
                Device.call_notifications_enabled.is_(True),
            )
        )
        if receiver is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "상대방의 통화 수신 기기가 등록되지 않았습니다"
            )
    source, questions = await questions_for_parent(request, session, parent.id)
    del source
    recording_enabled = await has_consent(session, parent.id)
    latest = await latest_consent(session, parent.id)
    disabled_reason = None
    if not recording_enabled:
        disabled_reason = "CONSENT_DENIED" if latest else "CONSENT_PENDING"
    call = CallRecord(
        parent_id=parent.id,
        child_id=user.id,
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
    voip_token = await session.scalar(
        select(Device.voip_token)
        .where(
            Device.user_id == parent.id,
            Device.platform == "IOS",
            Device.voip_token.is_not(None),
            Device.call_notifications_enabled.is_(True),
        )
        .order_by(Device.created_at.desc())
        .limit(1)
    )
    await session.commit()
    if voip_token:
        background.add_task(
            deliver_incoming_call_push,
            request.app.state.container.voip_push,
            voip_token,
            IncomingCallPush(
                call_id=call.id,
                caller_id=user.id,
                caller_name=user.name,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings_from(request).incoming_call_ttl_seconds),
            ),
            request.app.state.container,
        )
    elif settings_from(request).apns_voip_enabled:
        logger.warning("no iOS VoIP token registered for parent %s", parent.id)
    response = CallCreated(
        call_id=call.id,
        livekit_url=settings_from(request).livekit_url,
        room_name=call.room_name,
        access_token=token,
        recording_enabled=recording_enabled,
        recording_disabled_reason=disabled_reason,
        recording_disabled_message="동의가 완료되면 기록할 수 있어요" if disabled_reason else None,
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
) -> dict:
    require_role(user, UserRole.PARENT)
    call = await session.scalar(
        select(CallRecord).where(CallRecord.id == call_id).with_for_update()
    )
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    if call.parent_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "수신 권한이 없습니다")
    if call.state not in {CallState.RINGING.value, CallState.CREATED.value}:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 종료되었거나 응답한 통화입니다")
    settings = settings_from(request)
    if (
        datetime.now(UTC) - aware(call.started_at)
    ).total_seconds() >= settings.incoming_call_ttl_seconds:
        raise HTTPException(status.HTTP_410_GONE, "수신 대기 시간이 만료되었습니다")
    if call.recording_enabled and not await has_consent(session, call.parent_id):
        call.recording_enabled = False
        call.recording_disabled_reason = "CONSENT_DENIED"
    call.state = CallState.ACTIVE.value
    call.accepted_at = datetime.now(UTC)
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
        livekit_url=settings.livekit_url,
        room_name=call.room_name,
        access_token=token,
        raw_capture_required=call.recording_enabled,
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
    if user.id != call.parent_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "수신자만 거절할 수 있습니다")
    await session.rollback()
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
    call = await session.get(CallRecord, call_id)
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
    call = await session.get(CallRecord, call_id)
    asset = await session.get(AudioAsset, payload.asset_id)
    if call is None or asset is None or asset.call_id != call.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "오디오 자산을 찾을 수 없습니다")
    if user.id != call.parent_id or asset.kind != AssetKind.DEVICE_RAW.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "오디오 자산 접근 권한이 없습니다")
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


# Analysis
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


# Signal and reports
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


# LiveKit webhook and local health
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
