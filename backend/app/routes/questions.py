from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from sqlalchemy import func, select

from app.models import CallRecord, CallState, ParentProfile, QuestionTtsGrant, User
from app.routes.shared import aware, settings_from
from app.security import CurrentUser, SessionDep
from app.services.domain import ensure_report_access, has_consent, participants_consented
from app.services.questions import daily_questions
from app.services.storage import LocalStorage
from app.services.tts import ElevenLabsDirectTtsGateway, QuestionTtsError

router = APIRouter()
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
