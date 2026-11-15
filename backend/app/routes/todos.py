from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthDailySummary, ParentProfile, TodoItem, User
from app.routes.shared import aware
from app.schemas import TodoCreate, TodoUpdate
from app.security import CurrentUser, SessionDep
from app.services.reports import REPORT_TIMEZONE
from app.services.todos import daily_suggestions

router = APIRouter()

USER_SOURCE = "USER"
SUGGESTED_SOURCE = "SUGGESTED"
# 남아 있는(완료하지 않은) 직접 쓴 할 일 상한
MAX_OPEN_TODOS = 100
# 지난 추천과 끝낸 할 일은 이만큼 지나면 지운다(목록에는 오늘 것만 보인다)
KEEP_DAYS = 7


def todo_view(item: TodoItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "source": item.source,
        "done": item.done_at is not None,
        "doneAt": aware(item.done_at) if item.done_at else None,
        "createdAt": aware(item.created_at),
    }


async def recent_steps(session: AsyncSession, user_id: str, today: date) -> int | None:
    """어제·그제 하루 요약 중 가장 최근 걸음 수. 오늘 값은 하루가 덜 차서 보지 않는다."""
    return await session.scalar(
        select(HealthDailySummary.steps)
        .where(
            HealthDailySummary.user_id == user_id,
            HealthDailySummary.day >= today - timedelta(days=2),
            HealthDailySummary.day < today,
            HealthDailySummary.steps.is_not(None),
        )
        .order_by(HealthDailySummary.day.desc())
        .limit(1)
    )


async def own_item(session: AsyncSession, user_id: str, todo_id: str) -> TodoItem:
    item = await session.get(TodoItem, todo_id)
    if item is None or item.user_id != user_id or item.dismissed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "할 일을 찾을 수 없어요")
    return item


@router.get("/todos", tags=["Todo"])
async def list_todos(user: CurrentUser, session: SessionDep) -> dict:
    today = datetime.now(REPORT_TIMEZONE).date()
    start_of_today = datetime.combine(today, time.min, REPORT_TIMEZONE)
    # 같은 사람이 동시에 열어도 추천이 두 벌 생기지 않게 사람 단위로 줄 세운다.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    items = list(await session.scalars(select(TodoItem).where(TodoItem.user_id == user.id)))

    stale_day = today - timedelta(days=KEEP_DAYS)
    stale_before = start_of_today - timedelta(days=KEEP_DAYS)

    def stale(item: TodoItem) -> bool:
        if item.source == SUGGESTED_SOURCE and item.day is not None:
            return item.day < stale_day
        return item.done_at is not None and aware(item.done_at) < stale_before

    for item in [i for i in items if stale(i)]:
        await session.delete(item)
        items.remove(item)

    if not any(i.source == SUGGESTED_SOURCE and i.day == today for i in items):
        profile = await session.get(ParentProfile, user.id)
        steps = await recent_steps(session, user.id, today)
        conditions = profile.conditions if profile else []
        for key, text in daily_suggestions(user.id, today, conditions, steps):
            item = TodoItem(
                user_id=user.id, title=text, source=SUGGESTED_SOURCE, suggestion_key=key,
                day=today, dismissed=False, created_at=datetime.now(UTC),
            )
            session.add(item)
            items.append(item)
    await session.commit()

    def visible(item: TodoItem) -> bool:
        if item.dismissed:
            return False
        if item.source == SUGGESTED_SOURCE:
            return item.day == today
        return item.done_at is None or aware(item.done_at) >= start_of_today

    shown = sorted(
        (i for i in items if visible(i)),
        key=lambda i: (i.source != SUGGESTED_SOURCE, aware(i.created_at)),
    )
    return {"date": today.isoformat(), "items": [todo_view(i) for i in shown]}


@router.post("/todos", status_code=201, tags=["Todo"])
async def create_todo(payload: TodoCreate, user: CurrentUser, session: SessionDep) -> dict:
    open_count = len(list(await session.scalars(
        select(TodoItem.id).where(
            TodoItem.user_id == user.id,
            TodoItem.source == USER_SOURCE,
            TodoItem.done_at.is_(None),
        )
    )))
    if open_count >= MAX_OPEN_TODOS:
        raise HTTPException(status.HTTP_409_CONFLICT, "할 일은 100개까지 남겨 둘 수 있어요")
    item = TodoItem(
        user_id=user.id, title=payload.title, source=USER_SOURCE, created_at=datetime.now(UTC)
    )
    session.add(item)
    await session.commit()
    return todo_view(item)


@router.patch("/todos/{todoId}", tags=["Todo"])
async def update_todo(
    todo_id: Annotated[str, Path(alias="todoId", max_length=36)],
    payload: TodoUpdate,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    item = await own_item(session, user.id, todo_id)
    if payload.done and item.done_at is None:
        item.done_at = datetime.now(UTC)
    elif not payload.done:
        item.done_at = None
    await session.commit()
    return todo_view(item)


@router.delete("/todos/{todoId}", status_code=204, tags=["Todo"])
async def delete_todo(
    todo_id: Annotated[str, Path(alias="todoId", max_length=36)],
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    item = await own_item(session, user.id, todo_id)
    if item.source == SUGGESTED_SOURCE:
        # 지운 추천이 그날 다시 생기지 않게 표시만 남긴다.
        item.dismissed = True
    else:
        await session.delete(item)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
