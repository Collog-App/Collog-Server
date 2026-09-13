from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from sqlalchemy import select

from app.models import Baseline, ChangeSignal
from app.security import CurrentUser, SessionDep
from app.services.domain import ensure_report_access
from app.services.signals import baseline_to_dict, signal_to_dict

router = APIRouter()


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
