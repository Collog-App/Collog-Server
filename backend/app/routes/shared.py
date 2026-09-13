from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, Request, status

from app.config import Settings
from app.models import CallRecord, User
from app.security import SessionDep


def settings_from(request: Request) -> Settings:
    return request.app.state.container.settings


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def call_to_dict(call: CallRecord) -> dict:
    return {
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


async def ensure_call_access(session: SessionDep, user: User, call: CallRecord) -> None:
    if user.id not in {call.parent_id, call.child_id}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "통화 접근 권한이 없습니다")
