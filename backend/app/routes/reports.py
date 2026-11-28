from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Baseline, ChangeSignal, User
from app.security import CurrentUser, SessionDep
from app.services import billing
from app.services.domain import ensure_subject_access
from app.services.reports import recent_safety_notices
from app.services.signals import baseline_to_dict, signal_to_dict

router = APIRouter()


async def ensure_premium(session: AsyncSession, request: Request, user: User) -> None:
    """결제가 켜져 있고 보는 사람의 가족이 프리미엄이 아니면 402(403 은 공유 꺼짐)."""
    container = request.app.state.container
    people = await billing.people_of(session, user.id)
    plan = await billing.current_plan(
        session, container.settings, container.revenuecat, people, datetime.now(UTC)
    )
    if not plan.premium:
        raise HTTPException(402, billing.PREMIUM_REQUIRED_MESSAGE)


@router.get("/parents/{parentId}/baseline", tags=["Signal"])
async def get_baselines(
    parent_id: Annotated[str, Path(alias="parentId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    kind: Literal["ANCHOR", "ROLLING"] | None = None,
) -> dict:
    await ensure_subject_access(session, user, parent_id, "report")
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
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    filter: Literal["ALL", "PROMOTED", "ACUTE"] = "ALL",
) -> dict:
    await ensure_subject_access(session, user, parent_id, "report")
    # 급성 변화는 요금제와 상관없이 보인다(11-28 결정). 공유 꺼짐 검사는 위에서 먼저 한다.
    if filter != "ACUTE":
        await ensure_premium(session, request, user)
    statement = select(ChangeSignal).where(ChangeSignal.parent_id == parent_id)
    if filter == "PROMOTED":
        statement = statement.where(ChangeSignal.promoted.is_(True))
    elif filter == "ACUTE":
        statement = statement.where(ChangeSignal.acute.is_(True))
    items = list(await session.scalars(statement.order_by(ChangeSignal.observed_at.desc())))
    return {"signals": [signal_to_dict(item) for item in items]}


@router.get("/parents/{parentId}/safety-notices", tags=["Report"])
async def get_safety_notices(
    parent_id: Annotated[str, Path(alias="parentId")],
    user: CurrentUser,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> dict:
    """최근 days 일 동안 끝난 통화의 응급 안내. 요금제와 상관없이 보인다(11-28 결정).

    리포트의 safetyNotices 와 같은 항목이다. 공유 꺼짐(403)만 본다.
    """
    await ensure_subject_access(session, user, parent_id, "report")
    since = datetime.now(UTC) - timedelta(days=days)
    return {"safetyNotices": await recent_safety_notices(session, parent_id, since)}


@router.get("/parents/{parentId}/reports", tags=["Report"])
async def get_report(
    parent_id: Annotated[str, Path(alias="parentId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    period: Literal["WEEKLY", "MONTHLY"],
    date_: Annotated[date | None, Query(alias="date")] = None,
) -> dict:
    await ensure_subject_access(session, user, parent_id, "report")
    await ensure_premium(session, request, user)
    return await request.app.state.container.reports.get_or_issue(session, parent_id, period, date_)
