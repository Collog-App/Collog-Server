"""운영 지표 화면(/ops).

팀이 사업 지표(가입·가족 연결·통화 빈도·재사용·기능 사용)를 운영 DB 에서 바로 보는 곳이다.
화면은 숫자만 보여 주고 통화 내용·건강 수치·연락처는 내보내지 않는다. 계정 목록은 팀·시험
계정을 가려내는 데 필요한 닉네임까지만 보인다.

접속은 `OPS_ACCESS_CODES` 에 해시로 등록한 코드로만 한다(`X-Ops-Code` 헤더).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    CallRecord,
    CallState,
    ChangeSignal,
    Device,
    Family,
    FamilyMember,
    HealthDailySummary,
    HealthLinkConsent,
    HealthLinkDecision,
    OpsAccountFlag,
    ParentProfile,
    RefreshSession,
    Report,
    TodoItem,
    User,
    UserRole,
)
from app.security import SessionDep

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
STATIC = Path(__file__).with_name("ops_static")
TREND_WEEKS = 12

router = APIRouter(include_in_schema=False)


# ---------------------------------------------------------------- 접속


def match_access_code(settings: Settings, code: str | None) -> str | None:
    if not code:
        return None
    digest = hashlib.sha256(code.encode()).hexdigest()
    for label, expected in settings.ops_access_code_hashes.items():
        if hmac.compare_digest(digest, expected):
            return label
    return None


def enabled_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.container.settings
    if not settings.ops_access_code_hashes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return settings


def require_operator(
    request: Request,
    x_ops_code: Annotated[str | None, Header()] = None,
) -> str:
    label = match_access_code(enabled_settings(request), x_ops_code)
    if label is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "접속 코드가 맞지 않습니다")
    return label


OperatorDep = Annotated[str, Depends(require_operator)]


# ---------------------------------------------------------------- 계산 도우미


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def kst_day(value: datetime) -> date:
    return aware(value).astimezone(KST).date()


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def ratio(numerator: int, denominator: int) -> dict[str, Any]:
    pct = round(100 * numerator / denominator, 1) if denominator else None
    return {"n": numerator, "d": denominator, "pct": pct}


def age_band(birth: date | None, today: date) -> str:
    if birth is None:
        return "모름"
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    if age < 20:
        return "10대 이하"
    if age >= 80:
        return "80대 이상"
    return f"{age // 10 * 10}대"


def login_method(row: Any) -> str:
    if row.apple:
        return "Apple"
    if row.google:
        return "Google"
    if row.kakao:
        return "카카오"
    if row.phone:
        return "휴대폰"
    return "모름"


# ---------------------------------------------------------------- 원자료


class Dataset:
    """지표 계산에 필요한 열만 읽어 둔다. 콜록 규모에서는 파이썬에서 세는 편이 단순하다."""

    def __init__(self) -> None:
        self.users: dict[str, Any] = {}
        self.internal: set[str] = set()
        self.platform: dict[str, str] = {}
        self.last_seen: dict[str, datetime] = {}
        self.family_users: list[tuple[datetime, set[str]]] = []
        self.calls: list[Any] = []
        self.reports: list[Any] = []
        self.health_granted: set[str] = set()
        self.health_days: list[Any] = []
        self.todos: list[Any] = []
        self.profiles: list[Any] = []
        self.signals: list[Any] = []

    def touch(self, user_id: str | None, when: datetime | None) -> None:
        if user_id is None or when is None:
            return
        when = aware(when)
        if user_id not in self.last_seen or when > self.last_seen[user_id]:
            self.last_seen[user_id] = when


async def load_dataset(session: AsyncSession, settings: Settings) -> Dataset:
    data = Dataset()
    user_rows = await session.execute(
        select(
            User.id,
            User.role,
            User.nickname,
            User.gender,
            User.birth_date,
            User.marketing_opt_in,
            User.created_at,
            User.apple_subject.is_not(None).label("apple"),
            User.google_subject.is_not(None).label("google"),
            User.kakao_subject.is_not(None).label("kakao"),
            User.phone.is_not(None).label("phone"),
        )
    )
    for row in user_rows:
        data.users[row.id] = row
        data.touch(row.id, row.created_at)

    data.internal = set(
        await session.scalars(
            select(OpsAccountFlag.user_id).where(OpsAccountFlag.internal.is_(True))
        )
    )

    latest_device: dict[str, datetime] = {}
    for row in await session.execute(select(Device.user_id, Device.platform, Device.created_at)):
        created = aware(row.created_at)
        if row.user_id not in latest_device or created > latest_device[row.user_id]:
            latest_device[row.user_id] = created
            data.platform[row.user_id] = row.platform

    # 새로 고침 토큰은 같은 줄의 만료 시각을 「지금 + 보관 일수」로 미루므로, 만료 시각에서
    # 보관 일수를 빼면 마지막으로 앱이 토큰을 갱신한 시각이 된다.
    keep = timedelta(days=settings.refresh_ttl_days)
    for row in await session.execute(
        select(RefreshSession.user_id, RefreshSession.created_at, RefreshSession.expires_at)
    ):
        data.touch(row.user_id, row.created_at)
        data.touch(row.user_id, aware(row.expires_at) - keep)

    members: dict[str, set[str]] = defaultdict(set)
    for row in await session.execute(
        select(FamilyMember.family_id, FamilyMember.user_id).where(
            FamilyMember.user_id.is_not(None)
        )
    ):
        members[row.family_id].add(row.user_id)
    for row in await session.execute(select(Family.id, Family.created_by, Family.created_at)):
        data.family_users.append((aware(row.created_at), {row.created_by, *members[row.id]}))

    call_rows = await session.execute(
        select(
            CallRecord.parent_id,
            CallRecord.child_id,
            CallRecord.caller_id,
            CallRecord.state,
            CallRecord.started_at,
            CallRecord.accepted_at,
            CallRecord.duration_sec,
            CallRecord.parent_speech_sec,
            CallRecord.recording_enabled,
            CallRecord.report_notified_at,
        )
    )
    for row in call_rows:
        data.calls.append(row)
        data.touch(row.caller_id or row.child_id, row.started_at)
        if row.accepted_at is not None:
            data.touch(row.parent_id, row.accepted_at)
            data.touch(row.child_id, row.accepted_at)

    data.reports = list(await session.execute(select(Report.parent_id, Report.issued_at)))

    latest_consent: dict[str, tuple[datetime, int, str]] = {}
    for row in await session.execute(
        select(
            HealthLinkConsent.user_id,
            HealthLinkConsent.decision,
            HealthLinkConsent.agreed_at,
            HealthLinkConsent.seq,
        )
    ):
        key = (aware(row.agreed_at), row.seq or 0, row.decision)
        if row.user_id not in latest_consent or key[:2] > latest_consent[row.user_id][:2]:
            latest_consent[row.user_id] = key
    data.health_granted = {
        user_id
        for user_id, (_, _, decision) in latest_consent.items()
        if decision == HealthLinkDecision.GRANTED.value
    }

    for row in await session.execute(
        select(HealthDailySummary.user_id, HealthDailySummary.updated_at)
    ):
        data.health_days.append(row)
        data.touch(row.user_id, row.updated_at)

    for row in await session.execute(
        select(
            TodoItem.user_id,
            TodoItem.source,
            TodoItem.done_at,
            TodoItem.dismissed,
            TodoItem.created_at,
        )
    ):
        data.todos.append(row)
        if row.source == "USER":
            data.touch(row.user_id, row.created_at)
        data.touch(row.user_id, row.done_at)

    data.profiles = list(
        await session.execute(
            select(ParentProfile.parent_id, ParentProfile.conditions, ParentProfile.concerns)
        )
    )
    data.signals = list(
        await session.execute(
            select(ChangeSignal.parent_id, ChangeSignal.promoted, ChangeSignal.acute)
        )
    )
    return data


# ---------------------------------------------------------------- 지표


def included_users(data: Dataset, *, include_internal: bool, since: date | None) -> set[str]:
    return {
        user_id
        for user_id, row in data.users.items()
        if (include_internal or user_id not in data.internal)
        and (since is None or kst_day(row.created_at) >= since)
    }


def build_summary(
    data: Dataset,
    *,
    now: datetime,
    include_internal: bool,
    since: date | None,
) -> dict[str, Any]:
    keep = included_users(data, include_internal=include_internal, since=since)
    today = now.astimezone(KST).date()
    ago7, ago14, ago28, ago30 = (now - timedelta(days=d) for d in (7, 14, 28, 30))

    users = {uid: data.users[uid] for uid in keep}
    signed = {uid for uid, row in users.items() if row.nickname}
    parents = {uid for uid in signed if users[uid].role == UserRole.PARENT.value}
    children = signed - parents

    def created(uid: str) -> datetime:
        return aware(users[uid].created_at)

    def seen(uid: str) -> datetime:
        return data.last_seen.get(uid, created(uid))

    # --- 가족
    families = [
        (created_at, members & keep) for created_at, members in data.family_users if members & keep
    ]
    linked_families = [f for f in families if len(f[1]) >= 2]

    # --- 통화
    calls = [c for c in data.calls if c.parent_id in keep and c.child_id in keep]
    connected = [c for c in calls if c.accepted_at is not None]
    pair_days: dict[tuple[str, str], set[date]] = defaultdict(set)
    pair_first: dict[tuple[str, str], datetime] = {}
    for call in connected:
        pair = (call.parent_id, call.child_id)
        at = aware(call.accepted_at)
        pair_days[pair].add(kst_day(at))
        if pair not in pair_first or at < pair_first[pair]:
            pair_first[pair] = at

    # 첫 통화 뒤 14일 안에 다른 날 다시 통화했는가(같은 날 다시 건 것은 끊김 복구일 수 있어 뺀다)
    matured = [p for p, first in pair_first.items() if first <= ago14]
    repeated = [
        p
        for p in matured
        if any(
            kst_day(pair_first[p]) < d <= kst_day(pair_first[p] + timedelta(days=14))
            for d in pair_days[p]
        )
    ]

    # 최근 28일 쌍별 통화한 날 수 → 주당 통화 빈도
    window_start = kst_day(ago28)
    recent_days = {p: len([d for d in days if d > window_start]) for p, days in pair_days.items()}
    recent_days = {p: n for p, n in recent_days.items() if n}
    buckets = Counter(
        "1일" if n == 1 else "2~3일" if n <= 3 else "4~7일" if n <= 7 else "8일 이상"
        for n in recent_days.values()
    )
    per_week = [n / 4 for n in recent_days.values()]

    durations = [c.duration_sec for c in connected if c.duration_sec]
    speech = [c.parent_speech_sec for c in connected if c.parent_speech_sec]

    def funnel(rows: list[Any]) -> dict[str, Any]:
        accepted = [c for c in rows if c.accepted_at is not None]
        recorded = [c for c in accepted if c.recording_enabled]
        states = Counter(c.state for c in recorded)
        done = (
            states[CallState.ANALYZED.value]
            + states[CallState.ANALYSIS_EXCLUDED.value]
            + states[CallState.ANALYSIS_FAILED.value]
        )
        return {
            "attempts": len(rows),
            "connected": ratio(len(accepted), len(rows)),
            "recorded": ratio(len(recorded), len(accepted)),
            "analyzed": ratio(states[CallState.ANALYZED.value], done),
            "excluded": states[CallState.ANALYSIS_EXCLUDED.value],
            "failed": states[CallState.ANALYSIS_FAILED.value],
            "inProgress": len(recorded) - done,
            "reportNotified": len([c for c in recorded if c.report_notified_at is not None]),
        }

    # --- 기능
    reports = [r for r in data.reports if r.parent_id in keep]
    health_recent = {
        r.user_id for r in data.health_days if r.user_id in keep and aware(r.updated_at) >= ago7
    }
    todos = [t for t in data.todos if t.user_id in keep]
    user_todos = [t for t in todos if t.source == "USER" and not t.dismissed]
    profiles = [p for p in data.profiles if p.parent_id in keep]
    conditions = Counter(c for p in profiles for c in (p.conditions or []))
    signals = [s for s in data.signals if s.parent_id in keep]

    # --- 주별 추이(월요일 시작, 한국 시간)
    this_week = week_start(today)
    weeks = [this_week - timedelta(weeks=i) for i in range(TREND_WEEKS - 1, -1, -1)]
    signups_by_week = Counter(week_start(kst_day(created(uid))) for uid in signed)
    calls_by_week = Counter(week_start(kst_day(c.accepted_at)) for c in connected)
    analyzed_by_week = Counter(
        week_start(kst_day(c.accepted_at)) for c in connected if c.state == CallState.ANALYZED.value
    )
    pairs_by_week: dict[date, set[tuple[str, str]]] = defaultdict(set)
    for pair, days in pair_days.items():
        for d in days:
            pairs_by_week[week_start(d)].add(pair)

    old_enough = [uid for uid in signed if created(uid) <= ago7]
    return {
        "scope": {
            "includeInternal": include_internal,
            "since": since.isoformat() if since else None,
            "internalAccounts": len(data.internal & set(data.users)),
        },
        "headline": {
            "signedUp": len(signed),
            "signedUpParents": len(parents),
            "signedUpChildren": len(children),
            "loginOnly": len(set(users) - signed),
            "new7d": len([u for u in signed if created(u) >= ago7]),
            "new30d": len([u for u in signed if created(u) >= ago30]),
            "active7d": ratio(len([u for u in signed if seen(u) >= ago7]), len(signed)),
            "active30d": ratio(len([u for u in signed if seen(u) >= ago30]), len(signed)),
            "returnedAfter7d": ratio(
                len([u for u in old_enough if seen(u) >= created(u) + timedelta(days=7)]),
                len(old_enough),
            ),
            "families": len(families),
            "linkedFamilies": ratio(len(linked_families), len(families)),
            "callingPairs": len(pair_days),
            "callingPairs7d": len(
                [p for p, days in pair_days.items() if max(days) > kst_day(ago7)]
            ),
            "connectedCalls": len(connected),
            "connectedCalls7d": len([c for c in connected if aware(c.accepted_at) >= ago7]),
            "analyzedCalls": len([c for c in connected if c.state == CallState.ANALYZED.value]),
            "repeatWithin14d": ratio(len(repeated), len(matured)),
            "callDaysPerWeek": {
                "pairs": len(per_week),
                "mean": round(mean(per_week), 2) if per_week else None,
                "median": round(median(per_week), 2) if per_week else None,
                "buckets": {k: buckets.get(k, 0) for k in ("1일", "2~3일", "4~7일", "8일 이상")},
            },
        },
        "calls": {
            "all": funnel(calls),
            "last30d": funnel([c for c in calls if aware(c.started_at) >= ago30]),
            "durationSec": {
                "count": len(durations),
                "median": round(median(durations)) if durations else None,
                "mean": round(mean(durations)) if durations else None,
            },
            "parentSpeechSec": {
                "count": len(speech),
                "median": round(median(speech)) if speech else None,
            },
        },
        "features": {
            "reportsIssued": len(reports),
            "reportParents": len({r.parent_id for r in reports}),
            "reports30d": len([r for r in reports if aware(r.issued_at) >= ago30]),
            "healthLinked": ratio(len(data.health_granted & signed), len(signed)),
            "healthSynced7d": len(health_recent),
            "todoUsers": ratio(len({t.user_id for t in user_todos}), len(signed)),
            "todoDone": ratio(len([t for t in user_todos if t.done_at]), len(user_todos)),
            "suggestedDone": ratio(
                len([t for t in todos if t.source != "USER" and t.done_at]),
                len([t for t in todos if t.source != "USER"]),
            ),
            "conditions": dict(conditions.most_common()),
            "parentsWithProfile": len(profiles),
            "concernsWritten": len([p for p in profiles if (p.concerns or "").strip()]),
            "changeSignals": len(signals),
            "promotedSignals": len([s for s in signals if s.promoted]),
            "acuteSignals": len([s for s in signals if s.acute]),
            "marketingOptIn": ratio(
                len([u for u in signed if users[u].marketing_opt_in]), len(signed)
            ),
        },
        "people": {
            "platform": dict(Counter(data.platform.get(u, "모름") for u in signed).most_common()),
            "login": dict(Counter(login_method(users[u]) for u in signed).most_common()),
            "parentAge": dict(
                sorted(Counter(age_band(users[u].birth_date, today) for u in parents).items())
            ),
            "childAge": dict(
                sorted(Counter(age_band(users[u].birth_date, today) for u in children).items())
            ),
            "gender": dict(Counter(users[u].gender or "모름" for u in signed).most_common()),
        },
        "weekly": [
            {
                "week": week.isoformat(),
                "signups": signups_by_week.get(week, 0),
                "connectedCalls": calls_by_week.get(week, 0),
                "callingPairs": len(pairs_by_week.get(week, ())),
                "analyzedCalls": analyzed_by_week.get(week, 0),
            }
            for week in weeks
        ],
    }


def build_accounts(data: Dataset) -> list[dict[str, Any]]:
    calls = Counter()
    for call in data.calls:
        if call.accepted_at is not None:
            calls[call.parent_id] += 1
            calls[call.child_id] += 1
    rows = sorted(data.users.values(), key=lambda r: aware(r.created_at), reverse=True)
    return [
        {
            "id": row.id,
            "nickname": row.nickname,
            "role": row.role,
            "platform": data.platform.get(row.id),
            "login": login_method(row),
            "createdAt": aware(row.created_at).isoformat(),
            "lastSeenAt": data.last_seen.get(row.id, aware(row.created_at)).isoformat(),
            "connectedCalls": calls[row.id],
            "internal": row.id in data.internal,
        }
        for row in rows
    ]


# ---------------------------------------------------------------- 경로


PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
}


@router.get("/ops", response_class=HTMLResponse)
async def ops_page(settings: Annotated[Settings, Depends(enabled_settings)]) -> HTMLResponse:
    del settings
    return HTMLResponse((STATIC / "index.html").read_text("utf-8"), headers=PAGE_HEADERS)


@router.get("/ops/app.js")
async def ops_script(settings: Annotated[Settings, Depends(enabled_settings)]) -> Response:
    del settings
    return Response(
        (STATIC / "app.js").read_text("utf-8"),
        media_type="text/javascript",
        headers=PAGE_HEADERS,
    )


@router.get("/ops/app.css")
async def ops_style(settings: Annotated[Settings, Depends(enabled_settings)]) -> Response:
    del settings
    return Response(
        (STATIC / "app.css").read_text("utf-8"),
        media_type="text/css",
        headers=PAGE_HEADERS,
    )


@router.get("/ops/api/summary")
async def ops_summary(
    request: Request,
    operator: OperatorDep,
    session: SessionDep,
    internal: Annotated[bool, Query()] = False,
    since: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    settings: Settings = request.app.state.container.settings
    now = datetime.now(UTC)
    data = await load_dataset(session, settings)
    logger.info("ops summary viewed by %s", operator)
    return {
        "generatedAt": now.isoformat(),
        **build_summary(data, now=now, include_internal=internal, since=since),
    }


@router.get("/ops/api/accounts")
async def ops_accounts(
    request: Request, operator: OperatorDep, session: SessionDep
) -> dict[str, Any]:
    settings: Settings = request.app.state.container.settings
    data = await load_dataset(session, settings)
    logger.info("ops accounts viewed by %s", operator)
    return {"accounts": build_accounts(data)}


class InternalFlag(BaseModel):
    internal: bool


@router.put("/ops/api/accounts/{user_id}/internal")
async def ops_mark_internal(
    user_id: str, payload: InternalFlag, operator: OperatorDep, session: SessionDep
) -> dict[str, Any]:
    if await session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "계정을 찾을 수 없습니다")
    flag = await session.get(OpsAccountFlag, user_id)
    if flag is None:
        flag = OpsAccountFlag(user_id=user_id)
        session.add(flag)
    flag.internal = payload.internal
    flag.updated_at = datetime.now(UTC)
    await session.commit()
    logger.info("ops account %s internal=%s by %s", user_id, payload.internal, operator)
    return {"id": user_id, "internal": payload.internal}
