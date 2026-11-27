"""구독 결제 규칙: 결제 켜기 스위치, 가족 프리미엄 판정, 분석 분량(원장).

정본은 결제 약속 문서 §2 다. 결제가 꺼져 있으면(BILLING_LAUNCH_AT 이 비었거나 아직 오지 않음)
모두 프리미엄이고 잠금·분량 제한이 없다. 분량 원장은 꺼져 있어도 쌓는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    AnalysisUsage,
    CallRecord,
    OpsAccountFlag,
    RevenueCatEvent,
    Transcript,
    User,
    UserEntitlement,
)
from app.services.domain import family_for_user, family_people
from app.services.revenuecat import (
    EntitlementState,
    RevenueCatClient,
    RevenueCatError,
    entitlement_from_event,
    entitlement_from_subscriber,
    related_user_ids,
    text,
)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
QUOTA_EXCEEDED_REASON = "ANALYSIS_QUOTA_EXCEEDED"
PREMIUM_REQUIRED_MESSAGE = "주간 리포트는 프리미엄에서 볼 수 있어요."
# 만료가 막 지났는데 갱신될 구독은 웹훅을 놓쳤을 수 있어 판정 전에 다시 읽는다.
LAPSED_LOOKBACK = timedelta(days=7)
LAPSED_RECHECK = timedelta(minutes=10)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def billing_enabled(settings: Settings, now: datetime) -> bool:
    return settings.billing_launch_at is not None and now >= settings.billing_launch_at


def kst_month(moment: datetime) -> str:
    return aware(moment).astimezone(KST).strftime("%Y-%m")


def month_resets_at(moment: datetime) -> datetime:
    """다음 달 1일 00:00 (한국 시간)."""
    local = aware(moment).astimezone(KST)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=KST)


async def people_of(session: AsyncSession, user_id: str) -> set[str]:
    """그 사람이 속한 가족의 사람(만든 사람 + 수락한 구성원). 가족이 없으면 본인 하나."""
    family = await family_for_user(session, user_id)
    return await family_people(session, family) if family is not None else {user_id}


async def billing_start(
    session: AsyncSession, settings: Settings, people: set[str], now: datetime
) -> datetime | None:
    """그 가족에게 결제가 켜진 시각(UTC). 꺼져 있으면 None.

    전체 스위치(BILLING_LAUNCH_AT)가 지났거나, 가족 중 누구라도 운영 화면에서 「결제 미리 켜기」를
    한 계정이면 켜진다. 미리 켜기 가족은 가장 먼저 가입한 사람의 가입 시각부터 켜진 것으로 본다
    (오래된 심사 계정은 바로 무료가 되어 결제 화면을 끝까지 볼 수 있다).
    """
    preview = await session.scalar(
        select(OpsAccountFlag.user_id).where(
            OpsAccountFlag.user_id.in_(people), OpsAccountFlag.billing_preview.is_(True)
        ).limit(1)
    )
    if preview is not None:
        first_joined = await session.scalar(
            select(func.min(User.created_at)).where(User.id.in_(people))
        )
        if first_joined is not None:
            return aware(first_joined).astimezone(UTC)
    if billing_enabled(settings, now):
        assert settings.billing_launch_at is not None
        return settings.billing_launch_at.astimezone(UTC)
    return None


@dataclass(frozen=True)
class FamilyPlan:
    billing_enabled: bool
    premium: bool
    source: str | None
    trial_ends_at: datetime | None
    # 유효한 구독이 있는 가족 사람 → (그 구독 만료 시각, 자동 갱신 중인가)
    payers: dict[str, tuple[datetime, bool | None]] = field(default_factory=dict)
    # 이 가족에게 결제가 켜진 시각. 사용량은 이 뒤의 줄만 센다.
    started_at: datetime | None = None

    @property
    def latest_payer(self) -> str | None:
        """가족에서 가장 늦게 끝나는 구독의 사람."""
        return max(self.payers, key=lambda user_id: self.payers[user_id][0], default=None)


async def family_plan(
    session: AsyncSession, settings: Settings, people: set[str], now: datetime
) -> FamilyPlan:
    rows = await session.execute(
        select(
            UserEntitlement.user_id, UserEntitlement.active_until, UserEntitlement.will_renew
        ).where(
            UserEntitlement.user_id.in_(people),
            UserEntitlement.active_until.is_not(None),
        )
    )
    payers = {
        user_id: (aware(until), renew) for user_id, until, renew in rows.all()
        if aware(until) > now
    }
    started_at = await billing_start(session, settings, people, now)
    if started_at is None:
        return FamilyPlan(False, True, None, None, payers)
    # 가장 먼저 가입한 사람의 체험만 본다. 새 사람이 들어와도 가족 체험은 늘지 않는다.
    first_joined = await session.scalar(
        select(func.min(User.created_at)).where(User.id.in_(people))
    )
    trial_ends_at = (
        max(aware(first_joined).astimezone(UTC), started_at) + timedelta(days=settings.trial_days)
        if first_joined is not None
        else None
    )
    if payers:
        source: str | None = "PAID"
    elif trial_ends_at is not None and now < trial_ends_at:
        source = "TRIAL"
    else:
        source = None
    return FamilyPlan(True, source is not None, source, trial_ends_at, payers, started_at)


async def billing_on_for(
    session: AsyncSession, settings: Settings, user_id: str, now: datetime
) -> bool:
    """그 사람의 가족에게 결제가 켜져 있는가(전체 스위치 또는 결제 미리 켜기)."""
    people = await people_of(session, user_id)
    return await billing_start(session, settings, people, now) is not None


async def refresh_lapsed(
    session: AsyncSession, settings: Settings, client: RevenueCatClient | None,
    people: set[str], now: datetime,
) -> bool:
    """만료가 7일 안에 지났고 해지하지 않은 구독을 RevenueCat 에서 다시 읽는다.

    갱신 웹훅을 놓쳐 유료 사용자가 잠기지 않게 한다. 한 사람당 10분(updated_at)에 한 번만 부르고,
    실패해도 요청을 실패시키지 않고 저장값으로 판정한다. 다시 읽은 사람이 있으면 True.
    """
    if client is None or not client.configured:
        return False
    lapsed = list(await session.scalars(
        select(UserEntitlement.user_id).where(
            UserEntitlement.user_id.in_(people),
            UserEntitlement.active_until <= now,
            UserEntitlement.active_until > now - LAPSED_LOOKBACK,
            or_(UserEntitlement.will_renew.is_(None), UserEntitlement.will_renew.is_(True)),
            UserEntitlement.updated_at < now - LAPSED_RECHECK,
        )
    ))
    if not lapsed or await billing_start(session, settings, people, now) is None:
        return False
    for user_id in lapsed:
        try:
            await refresh_entitlement(session, settings, client, user_id, now)
        except RevenueCatError:
            logger.warning("만료가 지난 구독을 다시 읽지 못해 저장값으로 판정합니다")
            # 다음 시도도 10분 뒤에 하도록 시각만 남긴다.
            row = await session.get(UserEntitlement, user_id)
            if row is not None:
                row.updated_at = now
    await session.commit()
    return True


async def current_plan(
    session: AsyncSession, settings: Settings, client: RevenueCatClient | None,
    people: set[str], now: datetime,
) -> FamilyPlan:
    """프리미엄이 아니면 만료가 막 지난 구독을 다시 읽어 한 번 더 판정한다."""
    plan = await family_plan(session, settings, people, now)
    if not plan.premium and await refresh_lapsed(session, settings, client, people, now):
        plan = await family_plan(session, settings, people, now)
    return plan


def analysis_limit(settings: Settings, plan: FamilyPlan) -> int:
    return settings.premium_analysis_seconds if plan.premium else settings.free_analysis_seconds


async def used_seconds(
    session: AsyncSession, people: set[str], month: str, since: datetime | None
) -> int:
    """그 달 가족 사용량. since(그 가족에게 결제가 켜진 시각)가 있으면 그 뒤의 줄만 센다."""
    statement = select(func.coalesce(func.sum(AnalysisUsage.seconds), 0)).where(
        AnalysisUsage.month == month,
        or_(AnalysisUsage.parent_id.in_(people), AnalysisUsage.child_id.in_(people)),
    )
    if since is not None:
        statement = statement.where(AnalysisUsage.created_at >= since)
    return int(await session.scalar(statement) or 0)


async def quota_exceeded(
    session: AsyncSession, settings: Settings, call: CallRecord, now: datetime,
    client: RevenueCatClient | None = None,
) -> bool:
    """그 통화 가족에게 결제가 켜져 있고 이번 달 사용량이 한도 이상이면 True.

    이미 원장에 오른 통화(STT 를 시작한 뒤 재처리)는 끝까지 분석하므로 보지 않는다.
    """
    if await session.get(AnalysisUsage, call.id) is not None:
        return False
    people = await people_of(session, call.parent_id) | await people_of(session, call.child_id)
    plan = await family_plan(session, settings, people, now)
    if not plan.billing_enabled:
        return False
    used = await used_seconds(session, people, kst_month(now), plan.started_at)
    if used < analysis_limit(settings, plan):
        return False
    if await refresh_lapsed(session, settings, client, people, now):
        plan = await family_plan(session, settings, people, now)
    return used >= analysis_limit(settings, plan)


async def record_usage(
    session: AsyncSession, call: CallRecord, now: datetime
) -> None:
    """STT 를 시작하는 통화를 원장에 남긴다. 같은 통화는 한 번만 남는다."""
    dialect = (await session.connection()).dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    await session.execute(
        insert(AnalysisUsage)
        .values(
            call_id=call.id,
            parent_id=call.parent_id,
            child_id=call.child_id,
            month=kst_month(now),
            seconds=max(0, call.duration_sec or 0),
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["call_id"])
    )
    await session.commit()


async def release_usage(session: AsyncSession, call_id: str) -> None:
    """STT 를 끝내 마치지 못하고 ANALYSIS_FAILED 로 끝난 통화의 원장 줄을 지운다(커밋은 부른 쪽).

    전사가 저장됐으면 STT 는 끝난 것이라 그대로 둔다. 재시도를 기다리는 통화에는 부르지 않는다.
    """
    await session.execute(
        delete(AnalysisUsage).where(
            AnalysisUsage.call_id == call_id,
            ~select(Transcript.id).where(Transcript.call_id == call_id).exists(),
        )
    )


async def entitlement_view(
    session: AsyncSession, settings: Settings, user: User, now: datetime,
    client: RevenueCatClient | None = None,
) -> dict[str, Any]:
    people = await people_of(session, user.id)
    await refresh_lapsed(session, settings, client, people, now)
    plan = await family_plan(session, settings, people, now)
    month = kst_month(now)
    paid_by_me = user.id in plan.payers
    # 내가 결제했으면 내 구독, 아니면 가족에서 가장 늦게 끝나는 구독을 보인다.
    shown = user.id if paid_by_me else plan.latest_payer
    paid_until, will_renew = plan.payers[shown] if shown else (None, None)
    payer_name = None
    if shown and not paid_by_me:
        payer = await session.get(User, shown)
        payer_name = payer.display_name if payer is not None else None
    return {
        "billingEnabled": plan.billing_enabled,
        "plan": "PREMIUM" if plan.premium else "FREE",
        "source": plan.source,
        "trialEndsAt": plan.trial_ends_at,
        "paidUntil": paid_until,
        "paidByMe": paid_by_me,
        "willRenew": will_renew,
        "payerName": payer_name,
        "analysis": {
            "month": month,
            "usedSec": await used_seconds(session, people, month, plan.started_at),
            "limitSec": analysis_limit(settings, plan),
            "resetsAt": month_resets_at(now),
        },
    }


async def save_entitlement(
    session: AsyncSession, user_id: str, state: EntitlementState, now: datetime
) -> None:
    """한 사람의 구독을 덮어쓴다. refresh 와 웹훅이 동시에 와도 겹치지 않게 upsert 한다."""
    values = {
        "active_until": state.active_until,
        "store": state.store,
        "product_id": state.product_id,
        "environment": state.environment,
        "will_renew": state.will_renew,
        "updated_at": now,
    }
    dialect = (await session.connection()).dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    await session.execute(
        insert(UserEntitlement)
        .values(user_id=user_id, **values)
        .on_conflict_do_update(index_elements=["user_id"], set_=values)
    )


async def refresh_entitlement(
    session: AsyncSession, settings: Settings, client: RevenueCatClient, user_id: str,
    now: datetime,
) -> None:
    """RevenueCat 에서 다시 읽어 저장한다(커밋은 부른 쪽). RevenueCatError 는 그대로 낸다."""
    subscriber = await client.subscriber(user_id)
    state = entitlement_from_subscriber(
        subscriber,
        settings.revenuecat_entitlement_id,
        allow_sandbox=settings.revenuecat_allow_sandbox,
    )
    await save_entitlement(session, user_id, state, now)


async def save_event_entitlement(
    session: AsyncSession, user_id: str, state: EntitlementState, event_type: object,
    now: datetime,
) -> None:
    """비밀 키 없이 이벤트 본문만으로 저장한다.

    재시도·순서 뒤바뀜으로 옛 이벤트가 늦게 와도 접근이 줄지 않게, EXPIRATION 이 아니면 만료
    시각을 앞으로만 옮긴다.
    """
    if event_type != "EXPIRATION" and state.active_until is not None:
        stored = await session.scalar(
            select(UserEntitlement.active_until).where(UserEntitlement.user_id == user_id)
        )
        if stored is not None and aware(stored) > state.active_until:
            state = EntitlementState(
                active_until=aware(stored), store=state.store, product_id=state.product_id,
                environment=state.environment, will_renew=state.will_renew,
            )
    await save_entitlement(session, user_id, state, now)


async def apply_revenuecat_event(
    session: AsyncSession, settings: Settings, client: RevenueCatClient,
    event: dict[str, Any], now: datetime,
) -> str:
    """웹훅 이벤트 하나를 반영하고 커밋한다. 처리한 결과 이름을 돌려준다.

    접근을 끊는 것은 저장한 만료 시각이 지나서뿐이다. CANCELLATION·BILLING_ISSUE 는 만료 시각을
    그대로 두므로 그때까지 프리미엄이 유지된다.
    """
    event_id = str(event["id"])[:100]
    if await session.get(RevenueCatEvent, event_id) is not None:
        return "DUPLICATE"
    environment = text(event.get("environment"), 16)
    result = "IGNORED_SANDBOX"
    if environment != "SANDBOX" or settings.revenuecat_allow_sandbox:
        candidates = related_user_ids(event)
        users = (
            list(await session.scalars(select(User.id).where(User.id.in_(candidates))))
            if candidates
            else []
        )
        if client.configured:
            for user_id in users:
                await refresh_entitlement(session, settings, client, user_id, now)
        else:
            state = entitlement_from_event(event, settings.revenuecat_entitlement_id)
            if state is not None:
                for user_id in users:
                    await save_event_entitlement(session, user_id, state, event.get("type"), now)
            elif users:
                logger.info(
                    "RevenueCat %s 이벤트는 비밀 키 없이 반영할 내용이 없습니다", event.get("type")
                )
        result = "APPLIED" if users else "NO_USER"
    dialect = (await session.connection()).dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    await session.execute(
        insert(RevenueCatEvent)
        .values(
            id=event_id,
            type=text(event.get("type"), 40) or "UNKNOWN",
            app_user_id=text(event.get("app_user_id"), 100),
            environment=environment,
            received_at=now,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.commit()
    return result
