"""운영 지표 화면(/ops).

팀이 사업 지표(가입·가족 연결·통화 빈도·재사용·기능 사용)를 운영 DB 에서 바로 보는 곳이다.
화면은 숫자만 보여 주고 통화 내용·건강 수치·연락처는 내보내지 않는다. 계정 목록은 팀·시험
계정을 가려내는 데 필요한 닉네임까지만 보인다.

접속은 `OPS_ACCESS_CODES` 에 해시로 등록한 코드로만 한다(`X-Ops-Code` 헤더).
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    AcousticFeature,
    AppEvent,
    CallRecord,
    CallState,
    ChangeSignal,
    ConsentDecision,
    ConsentRecord,
    Device,
    Family,
    FamilyMember,
    HealthDailySummary,
    HealthExtraction,
    HealthLinkConsent,
    HealthLinkDecision,
    Metric,
    OpsAccountFlag,
    OpsCohort,
    ParentProfile,
    PushResult,
    RefreshSession,
    Report,
    TodoItem,
    Transcript,
    User,
    UserRole,
)
from app.security import SessionDep
from app.services.livekit import LiveKitError, LiveKitGateway

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
STATIC = Path(__file__).with_name("ops_static")
TREND_WEEKS = 12
COHORT_WEEKS = 8
COHORTS: tuple[str | None, ...] = (OpsCohort.AWARE.value, OpsCohort.UNAWARE.value, None)
# 운영 지표에 쓰는 로그인 뒤 이용 기록. 가입 단계 기록은 익명 줄까지 따로 읽고(load_dataset),
# 건강 연동·지표 설명 기록은 아직 화면에 없다.
OPS_EVENTS = ("app_open", "report_view", "post_call_answer", "incoming_call_shown",
              "call_connect_failed")
# 가입 단계 화면(onboarding_step 코드)을 앱 흐름 순서로. 이용 기록 약속 문서와 같다.
ONBOARDING_STEPS = ("WELCOME", "LOGIN", "TERMS", "AI_CONSENT", "PROFILE", "CONDITIONS",
                    "INVITE_CODE", "DONE")
PLATFORMS = ("IOS", "ANDROID")
# 분석 처리가 calls.processing_error 에 남기는 문구(services/pipeline.py) → 종류. 모르는 문구는
# OTHER 로만 보이고 원문은 화면·응답에 내보내지 않는다.
PROCESSING_ERROR_KINDS = {
    "Analysis retry pending": "RETRY_PENDING",
    "Analysis failed": "ANALYSIS_ERROR",
    "Analysis retry limit or audio retention limit reached": "RETRY_LIMIT",
    "부모 Egress 녹음이 완료되지 않았습니다": "PARENT_RECORDING_MISSING",
    "Raw audio upload expired": "RAW_UPLOAD_EXPIRED",
    "Audio retention limit reached": "AUDIO_EXPIRED",
}
# 휴지 비율 정의가 바뀌어(0018) 옛 값을 지운 표시. 측정하지 못한 것이 아니므로 비율에서 뺀다.
PAUSE_DEFINITION_CHANGED = "DEFINITION_CHANGED"
# 받는 앱은 울림 기록을 다음에 앱이 앞으로 올 때 올릴 수 있다. 「보냈는데 안 울림」은 통화가
# 시작되고 이만큼 지난 통화만 센다.
RING_REPORT_GRACE = timedelta(hours=1)

# 이 이름표의 접속 코드는 장애 알림(/ops/api/alerts)만 열 수 있다(바깥 감시 작업용).
ALERTS_ONLY_LABELS = frozenset({"monitor"})
# 장애 알림 기준
LIVEKIT_TIMEOUT_SECONDS = 5
ANALYSIS_BACKLOG_AFTER = timedelta(minutes=20)
FAILURE_STREAK = 3
FAILURE_STREAK_WITHIN = timedelta(hours=24)

router = APIRouter(include_in_schema=False)


# ---------------------------------------------------------------- 접속


def match_access_code(settings: Settings, code: str | None) -> str | None:
    if not code:
        return None
    digest = hashlib.sha256(code.encode()).hexdigest()
    for label, expected in settings.ops_access_code_hashes:
        if hmac.compare_digest(digest, expected):
            return label
    return None


def enabled_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.container.settings
    if not settings.ops_access_code_hashes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return settings


def require_alert_reader(
    request: Request,
    x_ops_code: Annotated[str | None, Header()] = None,
) -> str:
    label = match_access_code(enabled_settings(request), x_ops_code)
    if label is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "접속 코드가 맞지 않습니다")
    return label


def require_operator(label: Annotated[str, Depends(require_alert_reader)]) -> str:
    if label in ALERTS_ONLY_LABELS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이 접속 코드로는 볼 수 없습니다")
    return label


OperatorDep = Annotated[str, Depends(require_operator)]
AlertReaderDep = Annotated[str, Depends(require_alert_reader)]


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


def account_label(nickname: str | None, name: str | None) -> str:
    """운영자가 계정을 알아보도록 닉네임과 가입 이름(Apple·Google·전화)을 함께 보인다."""
    nickname = (nickname or "").strip()
    name = (name or "").strip()
    if nickname and name and nickname != name:
        return f"{nickname} ({name})"
    return nickname or name or "(이름 없음)"


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
        self.cohorts: dict[str, str] = {}
        self.events: list[Any] = []
        self.first_event: dict[str, datetime] = {}
        self.pushes: list[Any] = []
        self.consent_latest: dict[str, str] = {}
        self.inviters: set[str] = set()
        self.onboarding: list[Any] = []
        self.extraction_status: dict[str, str] = {}
        self.short_speech: set[str] = set()
        self.pause_ratio: dict[str, Any] = {}

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
            User.name,
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
    data.cohorts = {
        row.user_id: row.cohort
        for row in await session.execute(
            select(OpsAccountFlag.user_id, OpsAccountFlag.cohort).where(
                OpsAccountFlag.cohort.is_not(None)
            )
        )
    }

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
            CallRecord.id,
            CallRecord.processing_error,
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
            CallRecord.ended_at,
            CallRecord.analysis_timing,
        )
    )
    for row in call_rows:
        data.calls.append(row)
        data.touch(row.caller_id or row.child_id, row.started_at)
        if row.accepted_at is not None:
            data.touch(row.parent_id, row.accepted_at)
            data.touch(row.child_id, row.accepted_at)

    data.reports = list(
        await session.execute(select(Report.parent_id, Report.issued_at, Report.state))
    )

    # 분석 품질: 결과의 상태·이유 코드만 읽는다(추출 내용·전사·수치는 읽지 않음)
    data.extraction_status = {
        row.call_id: row.parse_status
        for row in await session.execute(
            select(HealthExtraction.call_id, HealthExtraction.parse_status)
        )
    }
    data.short_speech = set(
        await session.scalars(
            select(Transcript.call_id).where(
                Transcript.exclusion_reason == "INSUFFICIENT_PARENT_SPEECH"
            )
        )
    )
    data.pause_ratio = {
        row.call_id: row
        for row in await session.execute(
            select(
                AcousticFeature.call_id,
                AcousticFeature.status,
                AcousticFeature.unmeasurable_reason,
            ).where(AcousticFeature.metric == Metric.PAUSE_RATIO.value)
        )
    }

    # 외부 AI 동의: 사람마다 마지막 선택(문서 판과 상관없이)
    consent_at: dict[str, datetime] = {}
    for row in await session.execute(
        select(ConsentRecord.user_id, ConsentRecord.decision, ConsentRecord.agreed_at)
    ):
        at = aware(row.agreed_at)
        if row.user_id not in consent_at or at > consent_at[row.user_id]:
            consent_at[row.user_id] = at
            data.consent_latest[row.user_id] = row.decision
    # 초대한 사람은 남기지 않으므로 초대 줄(구성원)이 있는 가족을 만든 사람으로 본다
    data.inviters = set(
        await session.scalars(
            select(Family.created_by)
            .join(FamilyMember, FamilyMember.family_id == Family.id)
            .distinct()
        )
    )

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

    # 사람마다 마지막으로 올린 시각 하나만 읽는다(하루 요약 전체를 읽지 않음)
    for row in await session.execute(
        select(
            HealthDailySummary.user_id,
            func.max(HealthDailySummary.updated_at).label("updated_at"),
        ).group_by(HealthDailySummary.user_id)
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

    # 이용 기록은 「마지막 사용」 계산(last_seen)에 넣지 않는다. 기존 지표가 그대로 남게 한다.
    data.events = list(
        await session.execute(
            select(
                AppEvent.user_id,
                AppEvent.name,
                AppEvent.code,
                AppEvent.call_id,
                AppEvent.occurred_at,
            ).where(AppEvent.user_id.is_not(None), AppEvent.name.in_(OPS_EVENTS))
        )
    )
    data.first_event = {
        row.user_id: aware(row.first)
        for row in await session.execute(
            select(AppEvent.user_id, func.min(AppEvent.occurred_at).label("first"))
            .where(AppEvent.user_id.is_not(None))
            .group_by(AppEvent.user_id)
        )
    }
    # 로그인 전 단계는 익명 줄(user_id 없음)이다
    data.onboarding = list(
        await session.execute(
            select(
                AppEvent.user_id, AppEvent.code, AppEvent.platform, AppEvent.occurred_at
            ).where(AppEvent.name == "onboarding_step")
        )
    )
    data.pushes = [
        row
        for row in await session.execute(
            select(
                CallRecord.id,
                CallRecord.parent_id,
                CallRecord.child_id,
                CallRecord.caller_id,
                CallRecord.started_at,
                CallRecord.incoming_push,
            )
        )
        if row.incoming_push
    ]
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
    # iOS 는 닉네임 단계가 없다. 닉네임이 없어 「가입 중 멈춤」으로 보는 것은 안드로이드 가입
    # 흐름(Google 로그인 → 닉네임·약관)을 시작했다가 끝내지 않은 계정뿐이다.
    signed = {uid for uid, row in users.items() if row.nickname or not row.google}
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
    # 분석이 끝난 통화의 단계별 소요 시간(초). 이 기록이 생기기 전 통화는 빠진다.
    timings = [
        c.analysis_timing
        for c in connected
        if c.state == CallState.ANALYZED.value and c.analysis_timing
    ]
    # 사용자가 느끼는 시간: 통화가 끝나고 리포트 알림이 나가기까지.
    notify_waits = [
        (aware(c.report_notified_at) - aware(c.ended_at)).total_seconds()
        for c in connected
        if c.state == CallState.ANALYZED.value and c.report_notified_at and c.ended_at
    ]

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
            "analysisTime": {
                "count": len(timings),
                **{
                    key: round(median(t.get(key, 0) for t in timings)) if timings else None
                    for key in ("totalSec", "waitSec", "sttSec", "analysisSec")
                },
                "notifyCount": len(notify_waits),
                "notifySec": round(median(notify_waits)) if notify_waits else None,
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
            "label": account_label(row.nickname, row.name),
            "hasNickname": bool(row.nickname),
            "role": row.role,
            "platform": data.platform.get(row.id),
            "login": login_method(row),
            "createdAt": aware(row.created_at).isoformat(),
            "lastSeenAt": data.last_seen.get(row.id, aware(row.created_at)).isoformat(),
            "connectedCalls": calls[row.id],
            "internal": row.id in data.internal,
            "cohort": data.cohorts.get(row.id),
        }
        for row in rows
    ]


def build_cohorts(
    data: Dataset,
    *,
    now: datetime,
    include_internal: bool,
    since: date | None,
) -> dict[str, Any]:
    """테스트 그룹(인지·비인지·미정)별 통화·리포트·추천 질문·앱 사용.

    그룹 사람은 build_summary 의 가입 완료 사용자와 같은 기준(팀·시험 계정과 안드로이드 가입
    단계에서 멈춘 계정은 뺌)이고, 통화는 받아 연결된 통화만 센다.
    """
    keep = included_users(data, include_internal=include_internal, since=since)
    signed = {uid for uid in keep if data.users[uid].nickname or not data.users[uid].google}
    this_week = week_start(now.astimezone(KST).date())
    weeks = [this_week - timedelta(weeks=i) for i in range(COHORT_WEEKS - 1, -1, -1)]
    ago7 = now - timedelta(days=7)

    calls: list[tuple[date, set[str]]] = []
    call_times: dict[str, list[datetime]] = defaultdict(list)
    for call in data.calls:
        if call.accepted_at is None or call.parent_id not in keep or call.child_id not in keep:
            continue
        at = aware(call.accepted_at)
        calls.append((week_start(kst_day(at)), {call.parent_id, call.child_id}))
        for uid in (call.parent_id, call.child_id):
            call_times[uid].append(at)
    events: dict[str, list[Any]] = defaultdict(list)
    for event in data.events:
        if event.user_id in signed:
            events[event.user_id].append(event)

    groups = []
    for cohort in COHORTS:
        members = {uid for uid in signed if data.cohorts.get(uid) == cohort}
        people = len(members)
        called7d = {uid for uid in members if any(at >= ago7 for at in call_times[uid])}
        # 그 사람에게 받은 통화가 없던 한국 날짜에 리포트를 연 적이 있는가
        reopened = set()
        for uid in members:
            call_days = {kst_day(at) for at in call_times[uid]}
            if any(
                e.name == "report_view" and kst_day(e.occurred_at) not in call_days
                for e in events[uid]
            ):
                reopened.add(uid)
        answers = [e.code for uid in members for e in events[uid] if e.name == "post_call_answer"]
        weekly = []
        for week in weeks:
            in_week = [pair for start, pair in calls if start == week and pair & members]
            active = {
                uid
                for uid in members
                for e in events[uid]
                if e.name == "app_open" and week_start(kst_day(e.occurred_at)) == week
            }
            weekly.append({
                "week": week.isoformat(),
                "calls": len(in_week),
                "callsPerPerson": (
                    round(sum(len(pair & members) for pair in in_week) / people, 2)
                    if people else None
                ),
                "activeUsers": len(active),
            })
        groups.append({
            "cohort": cohort,
            "people": people,
            "calledLast7d": ratio(len(called7d), people),
            "reportOnNoCallDay": ratio(len(reopened), people),
            "questionUsed": ratio(answers.count("YES"), len(answers)),
            "weekly": weekly,
        })
    return {"weeks": [week.isoformat() for week in weeks], "groups": groups}


def build_delivery(
    data: Dataset,
    *,
    now: datetime,
    include_internal: bool,
    since: date | None,
) -> dict[str, Any]:
    """전화 알림 결과와 「보냈는데 안 울림」, 통화 연결 실패 코드.

    「안 울림」은 알림을 성공으로 보낸 통화 가운데 받는 사람 앱이 울림 기록
    (incoming_call_shown)을 남기지 않은 비율이다. 알림 결과를 남기기 시작한 뒤의 통화만, 그리고
    받는 사람 앱이 그 통화 전에 이용 기록을 보낸 적이 있는 통화만 센다(기록을 보내지 않는 옛
    앱이 안 울림으로 잡히지 않게).
    """
    keep = included_users(data, include_internal=include_internal, since=since)
    shown = {
        (e.user_id, e.call_id) for e in data.events if e.name == "incoming_call_shown" and e.call_id
    }
    results: Counter[str] = Counter()
    pushed = not_rung = 0
    for row in data.pushes:
        if row.parent_id not in keep or row.child_id not in keep:
            continue
        result = row.incoming_push.get("result")
        results[result] += 1
        started = aware(row.started_at)
        if result != PushResult.SENT.value or started > now - RING_REPORT_GRACE:
            continue
        caller = row.caller_id or row.child_id
        callee = row.child_id if caller == row.parent_id else row.parent_id
        rang = (callee, row.id) in shown
        first = data.first_event.get(callee)
        if not rang and (first is None or first > started):
            continue
        pushed += 1
        not_rung += not rang

    ago7 = now - timedelta(days=7)
    failures = [e for e in data.events if e.name == "call_connect_failed" and e.user_id in keep]
    return {
        "pushResults": {item.value: results.get(item.value, 0) for item in PushResult},
        "notRung": ratio(not_rung, pushed),
        "connectFailed": {
            "all": dict(Counter(e.code for e in failures).most_common()),
            "last7d": dict(
                Counter(e.code for e in failures if aware(e.occurred_at) >= ago7).most_common()
            ),
        },
    }


def build_signup(
    data: Dataset,
    *,
    now: datetime,
    include_internal: bool,
    since: date | None,
) -> dict[str, Any]:
    """가입 퍼널(단계마다 거기까지 온 사람 수)과 가입 단계 화면 기록 건수.

    퍼널은 가입 완료 사용자(build_summary 와 같은 기준) 가운데 각 단계를 한 사람을 따로 센다.
    초대를 받아 들어온 사람은 「초대 만듦」을 거치지 않으므로 앞 단계를 꼭 지나야 셈하지 않는다.
    """
    del now
    keep = included_users(data, include_internal=include_internal, since=since)
    signed = {uid for uid in keep if data.users[uid].nickname or not data.users[uid].google}

    connected: set[str] = set()
    for _, members in data.family_users:
        people = members & keep
        if len(people) >= 2:
            connected |= people
    calls = [
        c for c in data.calls
        if c.accepted_at is not None and c.parent_id in keep and c.child_id in keep
    ]
    called = {uid for c in calls for uid in (c.parent_id, c.child_id)}
    # 리포트는 분석 대상(부모 칸)마다 발행된다. 통화가 없어 빈 리포트(EMPTY)는 세지 않는다.
    subjects = {r.parent_id for r in data.reports if r.state != "EMPTY"}
    reported = subjects | {
        uid for c in calls if c.parent_id in subjects for uid in (c.parent_id, c.child_id)
    }

    def reached(people: set[str]) -> dict[str, Any]:
        return ratio(len(people & signed), len(signed))

    steps: Counter[tuple[str, str]] = Counter()
    anonymous: Counter[str] = Counter()
    for event in data.onboarding:
        if event.user_id is None:
            # 익명 줄은 계정이 없어 팀·시험 계정을 가릴 수 없다. 「가입일 이후」는 기록 날짜로 본다.
            if since is not None and kst_day(event.occurred_at) < since:
                continue
            anonymous[event.code] += 1
        elif event.user_id not in keep:
            continue
        steps[(event.code, event.platform)] += 1
    return {
        "signedUp": len(signed),
        "loginOnly": len(keep - signed),
        "stages": {
            "consentGranted": reached({
                uid for uid, decision in data.consent_latest.items()
                if decision == ConsentDecision.GRANTED.value
            }),
            "consentDenied": reached({
                uid for uid, decision in data.consent_latest.items()
                if decision == ConsentDecision.DENIED.value
            }),
            "conditions": reached({p.parent_id for p in data.profiles}),
            "inviteCreated": reached(data.inviters),
            "connected": reached(connected),
            "firstCall": reached(called),
            "firstReport": reached(reported),
        },
        "onboardingSteps": [
            {
                "step": step,
                **{platform: steps[(step, platform)] for platform in PLATFORMS},
                "anonymous": anonymous[step],
            }
            for step in ONBOARDING_STEPS
        ],
    }


def build_quality(
    data: Dataset,
    *,
    now: datetime,
    include_internal: bool,
    since: date | None,
) -> dict[str, Any]:
    """분석 품질: 처리 오류 종류, 추출 없이 완료된 통화, 휴지 비율 측정 불가, 말이 짧은 통화."""
    keep = included_users(data, include_internal=include_internal, since=since)
    calls = [c for c in data.calls if c.parent_id in keep and c.child_id in keep]
    ago30 = now - timedelta(days=30)

    def error_kinds(rows: list[Any]) -> dict[str, int]:
        return dict(
            Counter(
                PROCESSING_ERROR_KINDS.get(c.processing_error, "OTHER")
                for c in rows
                if c.processing_error
            ).most_common()
        )

    analyzed = [c for c in calls if c.state == CallState.ANALYZED.value]
    # 분석 완료인데 추출 결과 줄이 없거나 추출이 OK 가 아닌(FAILED 등) 통화
    no_extraction = [c for c in analyzed if data.extraction_status.get(c.id) != "OK"]
    pauses = [
        data.pause_ratio[c.id]
        for c in analyzed
        if c.id in data.pause_ratio
        and data.pause_ratio[c.id].unmeasurable_reason != PAUSE_DEFINITION_CHANGED
    ]
    unmeasurable = [p for p in pauses if p.status == "UNMEASURABLE"]
    recorded = [c for c in calls if c.accepted_at is not None and c.recording_enabled]
    short = [
        c for c in recorded
        if c.state == CallState.ANALYSIS_EXCLUDED.value and c.id in data.short_speech
    ]
    return {
        "processingErrors": {
            "all": error_kinds(calls),
            "last30d": error_kinds([c for c in calls if aware(c.started_at) >= ago30]),
        },
        "analyzedWithoutExtraction": ratio(len(no_extraction), len(analyzed)),
        "pauseUnmeasurable": ratio(len(unmeasurable), len(pauses)),
        "pauseUnmeasurableReasons": dict(
            Counter(p.unmeasurable_reason or "UNKNOWN" for p in unmeasurable).most_common()
        ),
        "shortSpeechExcluded": ratio(len(short), len(recorded)),
    }


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
    scope = {"now": now, "include_internal": internal, "since": since}
    return {
        "generatedAt": now.isoformat(),
        **build_summary(data, **scope),
        "cohorts": build_cohorts(data, **scope),
        "delivery": build_delivery(data, **scope),
        "signup": build_signup(data, **scope),
        "quality": build_quality(data, **scope),
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


class CohortFlag(BaseModel):
    cohort: OpsCohort | None


@router.put("/ops/api/accounts/{user_id}/cohort")
async def ops_set_cohort(
    user_id: str, payload: CohortFlag, operator: OperatorDep, session: SessionDep
) -> dict[str, Any]:
    if await session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "계정을 찾을 수 없습니다")
    flag = await session.get(OpsAccountFlag, user_id)
    if flag is None:
        flag = OpsAccountFlag(user_id=user_id, internal=False)
        session.add(flag)
    cohort = payload.cohort.value if payload.cohort else None
    flag.cohort = cohort
    flag.updated_at = datetime.now(UTC)
    await session.commit()
    logger.info("ops account %s cohort=%s by %s", user_id, cohort, operator)
    return {"id": user_id, "cohort": cohort}


# ---------------------------------------------------------------- 장애 알림


async def find_problems(
    session: AsyncSession, livekit: LiveKitGateway, now: datetime
) -> list[dict[str, str]]:
    problems = []
    try:
        async with asyncio.timeout(LIVEKIT_TIMEOUT_SECONDS):
            await livekit.check_reachable()
    except (LiveKitError, TimeoutError):
        logger.warning("ops alert: LiveKit check failed", exc_info=True)
        problems.append({
            "code": "LIVEKIT_UNREACHABLE",
            "message": (
                f"LiveKit 서버가 {LIVEKIT_TIMEOUT_SECONDS}초 안에 답하지 않습니다(통화 연결 불가)"
            ),
        })

    # 녹음된 통화가 끝났는데 분석 대기(ENDED)나 분석 중(PROCESSING)에 오래 머물러 있다
    waiting = await session.scalar(
        select(func.count(CallRecord.id)).where(
            CallRecord.recording_enabled.is_(True),
            CallRecord.state.in_([CallState.ENDED.value, CallState.PROCESSING.value]),
            CallRecord.ended_at <= now - ANALYSIS_BACKLOG_AFTER,
        )
    )
    if waiting:
        minutes = int(ANALYSIS_BACKLOG_AFTER.total_seconds() // 60)
        problems.append({
            "code": "ANALYSIS_BACKLOG",
            "message": (
                f"통화가 끝나고 {minutes}분 넘게 분석을 기다리는 통화가 {waiting}건 있습니다"
            ),
        })

    # 분석이 끝난(완료·실패) 통화를 통화 끝난 시각 순으로 본다. 분석 제외(짧은 통화 등)는 건너뛴다.
    latest = list(
        await session.execute(
            select(CallRecord.state, CallRecord.ended_at)
            .where(
                CallRecord.state.in_([CallState.ANALYZED.value, CallState.ANALYSIS_FAILED.value]),
                CallRecord.ended_at.is_not(None),
            )
            .order_by(CallRecord.ended_at.desc())
            .limit(FAILURE_STREAK)
        )
    )
    if (
        len(latest) == FAILURE_STREAK
        and all(row.state == CallState.ANALYSIS_FAILED.value for row in latest)
        and aware(latest[0].ended_at) >= now - FAILURE_STREAK_WITHIN
    ):
        problems.append({
            "code": "ANALYSIS_FAILING",
            "message": (
                f"가장 최근에 분석이 끝난 통화 {FAILURE_STREAK}건이 모두 분석에 실패했습니다"
            ),
        })
    return problems


@router.get("/ops/api/alerts")
async def ops_alerts(
    request: Request, reader: AlertReaderDep, session: SessionDep
) -> dict[str, Any]:
    problems = await find_problems(
        session, request.app.state.container.livekit, datetime.now(UTC)
    )
    if problems:
        logger.warning(
            "ops alerts checked by %s: %s", reader, ", ".join(p["code"] for p in problems)
        )
    return {"ok": not problems, "problems": problems}
