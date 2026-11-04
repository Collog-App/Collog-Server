from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import select

from app.config import Settings
from app.models import AnalysisMode, CallParticipant, CallRecord, User
from app.security import SessionDep
from app.services.domain import ensure_subject_access


def settings_from(request: Request) -> Settings:
    return request.app.state.container.settings


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def call_to_dict(call: CallRecord, participants: list[CallParticipant] | None = None) -> dict:
    view = {
        "callId": call.id,
        "parentId": call.parent_id,
        "childId": call.child_id,
        "callerId": call.effective_caller_id,
        "calleeId": call.callee_id,
        "state": call.state,
        "timeSlot": call.time_slot,
        "startedAt": call.started_at,
        "endedAt": call.ended_at,
        "durationSec": call.duration_sec,
        "recorded": call.recording_enabled,
        "recordingEnabled": call.recording_enabled,
        "recordingDisabledReason": call.recording_disabled_reason,
        "recordingDisabledMessage": (
            "녹음이 중단되어 이번 통화는 분석하지 않아요"
            if call.recording_disabled_reason == "RECORDING_INTERRUPTED"
            else "녹음과 AI 분석 없이 통화해요" if not call.recording_enabled else None
        ),
        "parentSpeechSec": call.parent_speech_sec,
        "askedQuestionIds": call.asked_question_ids,
        "rawAudioPurgedAt": call.raw_audio_purged_at,
    }
    # 두 사람을 분석한 통화만 참여자별 상태를 덧붙인다. SINGLE 응답은 그대로다.
    if call.analysis_mode == AnalysisMode.BOTH.value:
        view["analysisMode"] = call.analysis_mode
        view["participants"] = [
            {
                "userId": item.user_id,
                "role": item.role,
                "speakerLabel": item.speaker_label,
                "analysisStatus": item.analysis_status,
                "exclusionReason": item.exclusion_reason,
                "speechSec": item.speech_sec,
            }
            for item in participants or []
        ]
    return view


async def call_participants(
    session: SessionDep, calls: list[CallRecord]
) -> dict[str, list[CallParticipant]]:
    both_ids = [call.id for call in calls if call.analysis_mode == AnalysisMode.BOTH.value]
    rows: dict[str, list[CallParticipant]] = {}
    if both_ids:
        for item in await session.scalars(
            select(CallParticipant)
            .where(CallParticipant.call_id.in_(both_ids))
            .order_by(CallParticipant.role.desc())
        ):
            rows.setdefault(item.call_id, []).append(item)
    return rows


def raw_capture(call: CallRecord, user_id: str) -> bool:
    """원시 오디오를 올릴 사람. 두 사람을 분석하면 둘 다, 아니면 분석 대상만이다."""
    return user_id == call.parent_id or (
        call.analysis_mode == AnalysisMode.BOTH.value and user_id == call.child_id
    )


async def ensure_call_access(session: SessionDep, user: User, call: CallRecord) -> None:
    if user.id not in {call.parent_id, call.child_id}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "통화 접근 권한이 없습니다")


async def call_subject(
    session: SessionDep, user: User, call: CallRecord, subject_id: str | None
) -> str:
    """통화 분석 결과를 볼 대상. 비우면 지금처럼 `parent_id` 다.

    다른 사람의 결과를 보면 그 사람이 정한 리포트 공유 범위(S4)를 본다. SINGLE 통화를 대상을
    비우고 부르는 것은 예전과 같이 통화 참여자 확인만 한다.
    """
    await ensure_call_access(session, user, call)
    both = call.analysis_mode == AnalysisMode.BOTH.value
    if subject_id is None and not both:
        return call.parent_id
    subject_id = subject_id or call.parent_id
    if subject_id not in ({call.parent_id, call.child_id} if both else {call.parent_id}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이 통화의 분석 대상이 아닙니다")
    if subject_id != user.id:
        await ensure_subject_access(session, user, subject_id, "report")
    return subject_id
