from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import or_, select

from app.consent import CONSENT_ITEMS
from app.models import CallRecord, CallState, ConsentDecision, ConsentRecord, User
from app.routes.shared import settings_from
from app.schemas import ConsentSubmit
from app.security import CurrentUser, SessionDep
from app.services.domain import consent_is_current, latest_consent

router = APIRouter()


@router.get("/consents/document", tags=["Consent"])
async def consent_document(request: Request) -> dict:
    version = settings_from(request).consent_document_version
    return {
        "version": version,
        "fullText": (
            "녹음과 AI 분석은 선택 사항임. 두 참여자가 모두 동의한 통화에서만 음성을 녹음함. "
            "거절해도 가족 통화 이용 가능함. Deepgram에 부모와 자녀의 통화 음성을 보내 "
            "대화를 글로 변환함. Google Gemini에 두 참여자의 전사문과 그 안의 증상, 복약, "
            "활동, 수면 정보를 보내 건강 기록을 생성함. ElevenLabs에는 질문 문장을 보내 "
            "질문 음성을 생성하며, 아이폰 직접 요청 시 IP 주소가 전달됨. "
            "이름, 전화번호, Apple 로그인 토큰은 AI 요청에 포함하지 않지만 대화에 말한 "
            "개인정보는 음성과 전사문에 포함될 수 있음. 해외 제공사 서버에서 처리될 수 있음. "
            "부모의 건강 기록과 음성 특징값은 가족 자녀에게 제공함. "
            "의료 진단이나 치료 용도가 아님. "
            "콜록 원본 음성은 분석 후 삭제하며 실패하거나 남은 파일은 자동 정리함. "
            "전사문과 건강 기록은 계정 삭제 시까지 보관함. 외부 제공사의 보관 기간과 "
            "삭제 처리는 해당 계약 및 정책에 따르며 콜록 서버 삭제와 별개임. "
            "Deepgram 요청에는 모델 개선 참여 제외 옵션을 사용함. Google의 유료 서비스 "
            "데이터 처리 조건을 운영자가 확인하기 전에는 녹음과 건강 분석을 비활성화함. "
            "ElevenLabs는 모델 학습 이용을 제외한 계정 설정으로 사용함. "
            "외부 음성 서비스를 사용할 수 없으면 아이폰 기본 음성으로 재생함. "
            "설정에서 동의를 변경할 수 있으며 거절하면 이후 녹음과 AI 분석을 중단함."
        ),
        "collectedItems": [
            "통화 음성", "화자별 전사문", "증상", "복약", "활동", "수면", "음성 특징값",
            "질문 문장", "질문 음성 요청 시 IP 주소",
        ],
        "purpose": "가족 통화 기반 건강 변화 기록과 리포트 제공",
        "retentionPeriod": "전사문과 건강 기록은 계정 삭제 시까지 보관함",
        "rawAudioPolicy": "콜록 원본 음성은 분석 후 삭제함. 외부 서비스 보관 정책은 별도임",
        "requiredItems": CONSENT_ITEMS,
    }


@router.post("/consents", status_code=201, tags=["Consent"])
async def submit_consent(
    payload: ConsentSubmit, request: Request, user: CurrentUser, session: SessionDep
) -> dict:
    async with request.app.state.container.calls.reserve_participants([user.id]):
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())
        return await save_consent(payload, request, user, session)


async def save_consent(
    payload: ConsentSubmit, request: Request, user: CurrentUser, session: SessionDep
) -> dict:
    busy = await session.scalar(select(CallRecord.id).where(
        or_(CallRecord.parent_id == user.id, CallRecord.child_id == user.id),
        CallRecord.state.in_([
            CallState.CREATED.value, CallState.RINGING.value, CallState.ACTIVE.value,
        ]),
        CallRecord.ended_at.is_(None),
    ).limit(1))
    if busy is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "통화를 종료한 뒤 동의를 변경해주세요")
    settings = settings_from(request)
    if payload.document_version != settings.consent_document_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "최신 동의 안내를 다시 확인해주세요")
    if payload.decision == "GRANT" and not payload.scrolled_to_end:
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
    return consent_dict(record, settings.consent_document_version)


def consent_dict(record: ConsentRecord, version: str) -> dict:
    return {
        "consentId": record.id,
        "userId": record.user_id,
        "documentVersion": record.document_version,
        "status": record.decision,
        "agreedItems": record.agreed_items,
        "agreedAt": record.agreed_at,
        "isCurrent": consent_is_current(record, version),
        "currentDocumentVersion": version,
    }


@router.get("/consents/me", tags=["Consent"])
async def my_consent(request: Request, user: CurrentUser, session: SessionDep) -> dict:
    record = await latest_consent(session, user.id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "동의 기록이 없습니다")
    return consent_dict(record, settings_from(request).consent_document_version)
