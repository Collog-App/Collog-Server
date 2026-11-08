from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent import HEALTH_LINK_ITEMS, HEALTH_LINK_TITLE, HEALTH_LINK_VERSION
from app.models import HealthDailySummary, HealthLinkConsent, HealthLinkDecision, User
from app.routes.shared import aware
from app.schemas import HealthDailyUpload, HealthLinkRequest
from app.security import CurrentUser, SessionDep
from app.services.domain import ensure_subject_access
from app.services.reports import REPORT_TIMEZONE

router = APIRouter()

HEALTH_DISCLAIMER = "이 정보는 의료 진단이 아니며 건강 변화 참고용입니다."
MAX_UPLOAD_AGE_DAYS = 400
MAX_RANGE_DAYS = 62


class HealthApiError(HTTPException):
    """응답의 code를 상태 코드 기본값 대신 이 값으로 내보낸다(main.py 처리기)."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(status_code, message)
        self.error_code = error_code


def client_ip(request: Request) -> str | None:
    # 운영은 Caddy 뒤라 request.client가 프록시 주소다. 마지막 X-Forwarded-For 값은
    # Caddy가 붙인 실제 접속 주소다.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:45] or None
    return request.client.host if request.client else None


async def latest_link(session: AsyncSession, user_id: str) -> HealthLinkConsent | None:
    return await session.scalar(
        select(HealthLinkConsent)
        .where(HealthLinkConsent.user_id == user_id)
        .order_by(HealthLinkConsent.agreed_at.desc())
        .limit(1)
    )


def is_linked(record: HealthLinkConsent | None) -> bool:
    return (
        record is not None
        and record.decision == HealthLinkDecision.GRANTED.value
        and record.document_version == HEALTH_LINK_VERSION
    )


async def last_synced_day(session: AsyncSession, user_id: str) -> date | None:
    return await session.scalar(
        select(func.max(HealthDailySummary.day)).where(HealthDailySummary.user_id == user_id)
    )


async def link_view(session: AsyncSession, user_id: str) -> dict:
    record = await latest_link(session, user_id)
    connected = is_linked(record)
    return {
        "connected": connected,
        "consentVersion": record.document_version if connected else None,
        "connectedAt": aware(record.agreed_at) if connected else None,
        "document": {
            "version": HEALTH_LINK_VERSION,
            "title": HEALTH_LINK_TITLE,
            "items": HEALTH_LINK_ITEMS,
        },
        "lastSyncedDay": await last_synced_day(session, user_id) if connected else None,
    }


async def lock_user(session: AsyncSession, user: User) -> None:
    # 철회와 업로드가 겹쳐 지운 요약이 다시 생기지 않도록 한 사람의 요청을 줄 세운다.
    await session.scalar(select(User).where(User.id == user.id).with_for_update())


@router.get("/health/link", tags=["Health"])
async def get_health_link(user: CurrentUser, session: SessionDep) -> dict:
    return await link_view(session, user.id)


@router.post("/health/link", tags=["Health"])
async def link_health(
    payload: HealthLinkRequest, request: Request, user: CurrentUser, session: SessionDep
) -> dict:
    if payload.version != HEALTH_LINK_VERSION:
        raise HealthApiError(
            status.HTTP_409_CONFLICT,
            "HEALTH_CONSENT_VERSION_MISMATCH",
            "최신 건강 앱 연결 안내를 다시 확인해주세요",
        )
    await lock_user(session, user)
    session.add(HealthLinkConsent(
        user_id=user.id,
        document_version=payload.version,
        decision=HealthLinkDecision.GRANTED.value,
        platform=payload.platform,
        agreed_at=datetime.now(UTC),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    ))
    await session.commit()
    return await link_view(session, user.id)


@router.delete("/health/link", status_code=204, tags=["Health"])
async def unlink_health(request: Request, user: CurrentUser, session: SessionDep) -> Response:
    await lock_user(session, user)
    record = await latest_link(session, user.id)
    if record is not None and record.decision == HealthLinkDecision.GRANTED.value:
        session.add(HealthLinkConsent(
            user_id=user.id,
            document_version=record.document_version,
            decision=HealthLinkDecision.REVOKED.value,
            platform=record.platform,
            agreed_at=datetime.now(UTC),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:255] or None,
        ))
    await session.execute(delete(HealthDailySummary).where(HealthDailySummary.user_id == user.id))
    await session.commit()
    return Response(status_code=204)


@router.put("/health/daily", tags=["Health"])
async def put_health_daily(
    payload: HealthDailyUpload, user: CurrentUser, session: SessionDep
) -> dict:
    await lock_user(session, user)
    if not is_linked(await latest_link(session, user.id)):
        raise HealthApiError(
            status.HTTP_403_FORBIDDEN, "HEALTH_LINK_REQUIRED", "건강 앱을 먼저 연결해주세요"
        )
    today = datetime.now(REPORT_TIMEZONE).date()
    oldest = today - timedelta(days=MAX_UPLOAD_AGE_DAYS)
    if any(not oldest <= item.day <= today for item in payload.days):
        raise HTTPException(422, "보낼 수 없는 날짜가 있어요")
    now = datetime.now(UTC)
    for item in payload.days:
        row = await session.get(HealthDailySummary, (user.id, item.day))
        if row is None:
            row = HealthDailySummary(user_id=user.id, day=item.day)
            session.add(row)
        row.platform = payload.platform
        row.sleep_minutes = item.sleep_minutes
        row.steps = item.steps
        row.resting_heart_rate = item.resting_heart_rate
        row.blood_pressure_systolic = item.blood_pressure_systolic
        row.blood_pressure_diastolic = item.blood_pressure_diastolic
        row.blood_glucose_mg_dl = item.blood_glucose_mg_dl
        row.updated_at = now
    await session.commit()
    return {"saved": len(payload.days), "lastSyncedDay": await last_synced_day(session, user.id)}


@router.get("/parents/{parentId}/health/daily", tags=["Health"])
async def get_parent_health_daily(
    parent_id: Annotated[str, Path(alias="parentId")],
    user: CurrentUser,
    session: SessionDep,
    from_: Annotated[date, Query(alias="from")],
    to: date,
) -> dict:
    await ensure_subject_access(session, user, parent_id, "report")
    if from_ > to or (to - from_).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            422, f"기간은 {MAX_RANGE_DAYS}일까지 볼 수 있어요"
        )
    record = await latest_link(session, parent_id)
    connected = is_linked(record)
    rows: list[HealthDailySummary] = []
    if connected:
        rows = list(await session.scalars(
            select(HealthDailySummary)
            .where(
                HealthDailySummary.user_id == parent_id,
                HealthDailySummary.day >= from_,
                HealthDailySummary.day <= to,
            )
            .order_by(HealthDailySummary.day)
        ))
    return {
        "parentId": parent_id,
        "connected": connected,
        "platform": record.platform if connected else None,
        "lastSyncedDay": await last_synced_day(session, parent_id) if connected else None,
        "days": [
            {
                "date": row.day,
                "sleepMinutes": row.sleep_minutes,
                "steps": row.steps,
                "restingHeartRate": row.resting_heart_rate,
                "bloodPressureSystolic": row.blood_pressure_systolic,
                "bloodPressureDiastolic": row.blood_pressure_diastolic,
                "bloodGlucoseMgDl": row.blood_glucose_mg_dl,
            }
            for row in rows
        ],
        "disclaimer": HEALTH_DISCLAIMER,
    }
