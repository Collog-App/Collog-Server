from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from sqlalchemy import select

from app.models import (
    AcousticAnalysisRun,
    AcousticFeature,
    AnalysisMode,
    CallParticipant,
    CallRecord,
    ExtractionEvidence,
    HealthExtraction,
    ParticipantStatus,
    RepeatEvent,
    Transcript,
)
from app.routes.shared import call_subject
from app.security import CurrentUser, SessionDep
from app.services.repeat_detector import repeat_rate_per_minute

router = APIRouter()

# 두 사람을 분석한 통화에서 볼 대상. 비우면 지금처럼 calls.parent_id 다.
SubjectQuery = Annotated[str | None, Query(alias="subjectId")]


def of_subject(statement, model, call: CallRecord, subject_id: str):
    """BOTH 통화는 대상 몫의 행만 고른다. SINGLE 통화의 행은 모두 parent_id 의 것이다."""
    if call.analysis_mode == AnalysisMode.BOTH.value:
        return statement.where(model.subject_user_id == subject_id)
    return statement


@router.get("/calls/{callId}/transcript", tags=["Analysis"])
async def get_transcript(
    call_id: Annotated[str, Path(alias="callId")],
    user: CurrentUser,
    session: SessionDep,
    subject_id: SubjectQuery = None,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    subject_id = await call_subject(session, user, call, subject_id)
    item = await session.scalar(select(Transcript).where(Transcript.call_id == call_id))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전사 결과가 아직 없습니다")
    excluded, exclusion_reason, speech_sec = (
        item.excluded, item.exclusion_reason, item.parent_speech_sec
    )
    if call.analysis_mode == AnalysisMode.BOTH.value:
        # 전사는 두 사람 발화를 함께 주고, 제외 여부와 발화 시간은 대상 기준으로 준다.
        participant = await session.scalar(
            select(CallParticipant).where(
                CallParticipant.call_id == call_id, CallParticipant.user_id == subject_id
            )
        )
        excluded = participant.analysis_status == ParticipantStatus.EXCLUDED.value
        exclusion_reason = participant.exclusion_reason if excluded else None
        speech_sec = participant.speech_sec or 0
    repeat_events = list(
        await session.scalars(
            of_subject(
                select(RepeatEvent).where(RepeatEvent.call_id == call_id),
                RepeatEvent, call, subject_id,
            ).order_by(RepeatEvent.start_ms)
        )
    )
    return {
        "callId": call_id,
        "provider": item.provider,
        "excluded": excluded,
        "exclusionReason": exclusion_reason,
        "parentSpeechSec": speech_sec,
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
        "repeatRequestsPerMinute": repeat_rate_per_minute(len(repeat_events), speech_sec),
    }


@router.get("/calls/{callId}/extraction", tags=["Analysis"])
async def get_extraction(
    call_id: Annotated[str, Path(alias="callId")],
    user: CurrentUser,
    session: SessionDep,
    subject_id: SubjectQuery = None,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    subject_id = await call_subject(session, user, call, subject_id)
    item = await session.scalar(of_subject(
        select(HealthExtraction).where(HealthExtraction.call_id == call_id),
        HealthExtraction, call, subject_id,
    ))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "추출 결과가 아직 없습니다")
    evidence = await session.scalar(of_subject(
        select(ExtractionEvidence).where(ExtractionEvidence.call_id == call_id),
        ExtractionEvidence, call, subject_id,
    ))
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
    subject_id: SubjectQuery = None,
) -> dict:
    call = await session.get(CallRecord, call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    subject_id = await call_subject(session, user, call, subject_id)
    items = list(await session.scalars(of_subject(
        select(AcousticFeature).where(AcousticFeature.call_id == call_id),
        AcousticFeature, call, subject_id,
    )))
    if not items:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "음향 분석 결과가 아직 없습니다")
    analysis_run = await session.scalar(of_subject(
        select(AcousticAnalysisRun).where(AcousticAnalysisRun.call_id == call_id),
        AcousticAnalysisRun, call, subject_id,
    ))
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
