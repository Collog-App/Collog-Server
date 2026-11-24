"""앱 이용 기록(app_events)의 받기 규칙과 보관 기간 정리.

이벤트 이름·코드값은 서버·iOS·안드로이드가 함께 쓰는 약속(이용 기록 약속 문서)과 같아야 한다.
모르는 이름·코드나 너무 옛·미래 시각은 오류로 돌려주지 않고 버린다. 앱은 4xx 를 받으면 묶음
전체를 버리므로, 한 줄 때문에 나머지 기록을 잃지 않게 하려는 것이다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Database
from app.models import AppEvent, CallRecord, Family, FamilyMember, User
from app.schemas import AppEventIn
from app.services.domain import connected_family_ids

logger = logging.getLogger(__name__)

# code 가 없는 이벤트는 None, 정해진 값만 받는 이벤트는 집합, 꼴만 보는 이벤트는 정규식.
EVENT_CODES: dict[str, frozenset[str] | re.Pattern[str] | None] = {
    "app_open": None,
    "report_view": frozenset({"DIRECT", "NOTIFICATION"}),
    "post_call_answer": frozenset({"YES", "NO", "NOT_SEEN"}),
    "incoming_call_shown": None,
    "call_connect_failed": frozenset({"MIC_PERMISSION", "NETWORK", "TOKEN", "SERVER", "OTHER"}),
    "onboarding_step": frozenset({
        "WELCOME", "LOGIN", "TERMS", "AI_CONSENT", "PROFILE", "CONDITIONS", "INVITE_CODE", "DONE",
    }),
    "health_link_result": frozenset({"CONNECTED", "PERMISSION_DENIED", "CONNECTED_NO_RECORDS"}),
    # 지표 이름(대문자·밑줄). 숫자는 F0_VARIATION 처럼 글자 사이에 한 개씩만 둔다
    # (전화번호 같은 숫자열은 받지 않는다).
    "metric_help_open": re.compile(r"(?=.{1,40}$)[A-Z_]+(?:[0-9][A-Z_]+)*"),
}
# 「버전 (빌드)」 꼴만 받는다(예: 1.2 (44)). 다른 글자(NUL 등)가 섞인 줄은 버린다.
APP_VERSION = re.compile(r"[0-9]{1,4}(\.[0-9]{1,4}){0,3} \([0-9]{1,9}\)")
# 사용자·통화 ID 는 UUID 꼴이다. 다른 글자가 섞인 값은 DB 에 묻지 않고 비운다.
RECORD_ID = re.compile(r"[0-9A-Za-z-]{1,36}")
SUBJECT_EVENTS = frozenset({"report_view", "post_call_answer"})
CALL_EVENTS = frozenset({"post_call_answer", "incoming_call_shown", "call_connect_failed"})
# 요청한 사람이 한쪽인 통화 ID 가 있어야 받는 이벤트(없거나 남의 통화면 그 줄을 버린다)
CALL_REQUIRED_EVENTS = frozenset({"post_call_answer"})
ANONYMOUS_EVENTS = frozenset({"onboarding_step"})

MAX_AGE = timedelta(days=30)
MAX_AHEAD = timedelta(minutes=5)
# 인증 없는 주소로 남는 줄의 상한(모든 기기 합). 넘치면 그 사이 익명 기록은 조용히 버린다.
ANONYMOUS_WINDOW = timedelta(minutes=10)
ANONYMOUS_WINDOW_LIMIT = 200
# 로그인한 사람 한 명이 남길 수 있는 줄의 상한. 넘친 줄은 조용히 버린다.
USER_WINDOW = timedelta(hours=24)
USER_WINDOW_LIMIT = 500

RETENTION = timedelta(days=365)
RETENTION_INTERVAL = timedelta(hours=1)


def code_allowed(name: str, code: str | None) -> bool:
    rule = EVENT_CODES[name]
    if rule is None:
        return code is None
    if isinstance(rule, re.Pattern):
        return code is not None and rule.fullmatch(code) is not None
    return code in rule


def utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def record_id(value: str | None) -> str | None:
    """빈 문자열과 ID 꼴이 아닌 값은 없는 것으로 본다."""
    return value if value and RECORD_ID.fullmatch(value) else None


def accepted_row(
    event: AppEventIn, *, user_id: str | None, now: datetime
) -> dict[str, object] | None:
    allowed_names = ANONYMOUS_EVENTS if user_id is None else EVENT_CODES
    code = event.code or None
    occurred_at = utc(event.at)
    if (
        event.name not in allowed_names
        or not code_allowed(event.name, code)
        or not APP_VERSION.fullmatch(event.app_version)
        or not now - MAX_AGE <= occurred_at <= now + MAX_AHEAD
    ):
        return None
    signed_in = user_id is not None
    subject_id = record_id(event.subject_id)
    call_id = record_id(event.call_id)
    if event.name in CALL_REQUIRED_EVENTS and call_id is None:
        return None
    return {
        "id": str(event.id),
        "user_id": user_id,
        "name": event.name,
        "code": code,
        "subject_id": subject_id if signed_in and event.name in SUBJECT_EVENTS else None,
        "call_id": call_id if signed_in and event.name in CALL_EVENTS else None,
        "platform": event.platform,
        "app_version": event.app_version,
        "occurred_at": occurred_at,
        "received_at": now,
    }


async def family_people_ids(session: AsyncSession, user_id: str) -> set[str]:
    """본인과 같은 가족(만든 사람·초대를 수락한 구성원)의 사용자 ID."""
    families = connected_family_ids(user_id)
    members = await session.scalars(
        select(FamilyMember.user_id).where(
            FamilyMember.family_id.in_(families), FamilyMember.user_id.is_not(None)
        )
    )
    owners = await session.scalars(select(Family.created_by).where(Family.id.in_(families)))
    return {user_id, *members, *owners}


async def store_events(
    session: AsyncSession, events: Sequence[AppEventIn], *, user_id: str | None
) -> int:
    """규칙에 맞는 기록만 남기고 그 수를 돌려준다. 이미 받은 id 는 다시 쓰지 않는다."""
    now = datetime.now(UTC)
    rows: dict[str, dict[str, object]] = {}
    for event in events:
        row = accepted_row(event, user_id=user_id, now=now)
        if row is not None:
            rows.setdefault(str(row["id"]), row)
    accepted = list(rows.values())

    if user_id is not None:
        if any(row["subject_id"] for row in accepted):
            people = await family_people_ids(session, user_id)
            for row in accepted:
                if row["subject_id"] not in people:
                    row["subject_id"] = None
        call_ids = {row["call_id"] for row in accepted if row["call_id"]}
        if call_ids:
            own_calls = set(await session.scalars(
                select(CallRecord.id).where(
                    CallRecord.id.in_(call_ids),
                    or_(CallRecord.parent_id == user_id, CallRecord.child_id == user_id),
                )
            ))
            for row in accepted:
                if row["call_id"] not in own_calls:
                    row["call_id"] = None
        accepted = [
            row for row in accepted
            if row["call_id"] or row["name"] not in CALL_REQUIRED_EVENTS
        ]
        if accepted:
            recent = await session.scalar(
                select(func.count()).select_from(AppEvent).where(
                    AppEvent.user_id == user_id, AppEvent.received_at >= now - USER_WINDOW
                )
            )
            accepted = accepted[: max(0, USER_WINDOW_LIMIT - (recent or 0))]
    elif accepted:
        recent = await session.scalar(
            select(func.count()).select_from(AppEvent).where(
                AppEvent.user_id.is_(None), AppEvent.received_at >= now - ANONYMOUS_WINDOW
            )
        )
        accepted = accepted[: max(0, ANONYMOUS_WINDOW_LIMIT - (recent or 0))]

    if accepted:
        dialect = (await session.connection()).dialect.name
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
        await session.execute(
            insert(AppEvent).values(accepted).on_conflict_do_nothing(index_elements=["id"])
        )
        await session.commit()
    return len(accepted)


class EventRetention:
    """보관 기간(1년)이 지난 이용 기록과, 이미 지워진 계정의 기록을 지운다.

    계정 삭제는 그 사람의 줄을 함께 지우지만, 배포가 이 표를 모르는 이전 판으로 되돌아간 동안
    지운 계정의 줄은 남는다. 정기 작업이 자주 불러도 한 시간에 한 번만 돈다.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self._last_run: datetime | None = None

    async def maintain(self) -> int:
        now = datetime.now(UTC)
        if self._last_run is not None and now - self._last_run < RETENTION_INTERVAL:
            return 0
        async with self.database.sessions() as session:
            expired = await session.execute(
                delete(AppEvent).where(AppEvent.occurred_at < now - RETENTION)
            )
            orphaned = await session.execute(
                delete(AppEvent).where(
                    AppEvent.user_id.is_not(None),
                    ~select(User.id).where(User.id == AppEvent.user_id).exists(),
                )
            )
            await session.commit()
        self._last_run = now
        if expired.rowcount:
            logger.info("보관 기간이 지난 이용 기록 %d건을 지웠습니다", expired.rowcount)
        if orphaned.rowcount:
            logger.info("지워진 계정의 이용 기록 %d건을 지웠습니다", orphaned.rowcount)
        return expired.rowcount + orphaned.rowcount
