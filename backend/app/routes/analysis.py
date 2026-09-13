from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from sqlalchemy import select

from app.models import (
    AcousticAnalysisRun,
    AcousticFeature,
    CallRecord,
    ExtractionEvidence,
    HealthExtraction,
    RepeatEvent,
    Transcript,
)
from app.routes.shared import ensure_call_access
from app.security import CurrentUser, SessionDep
from app.services.repeat_detector import repeat_rate_per_minute

router = APIRouter()


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
