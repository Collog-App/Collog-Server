from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AcousticFeature,
    Baseline,
    CallRecord,
    ChangeSignal,
    HealthExtraction,
    RepeatEvent,
    Report,
)
from app.services.signals import signal_to_dict

DISCLAIMER = "이 리포트는 의료 진단이 아니며 건강 변화 참고용입니다."
ADVISORY = "변화가 지속되거나 불편함이 있으면 의료진과 상담해보세요."


# 「이번 주」는 사용자가 사는 날짜 기준이다. 통화 시간대(MORNING/AFTERNOON_EVENING)도
# 같은 기준으로 판정한다(api.py). 기간 경계와 조회 범위가 서로 다른 타임존을 쓰면
# 한국처럼 UTC보다 앞선 지역에서 자정~오전 9시 사이에 이번 주 통화가 통째로 빠진다.
REPORT_TIMEZONE = ZoneInfo("Asia/Seoul")


def period_bounds(period: str, target: date | None) -> tuple[date, date]:
    target = target or datetime.now(REPORT_TIMEZONE).date()
    if period == "WEEKLY":
        start = target - timedelta(days=target.weekday())
        return start, start + timedelta(days=6)
    last_day = calendar.monthrange(target.year, target.month)[1]
    return target.replace(day=1), target.replace(day=last_day)


class ReportService:
    async def get_or_issue(
        self, session: AsyncSession, parent_id: str, period: str, target: date | None
    ) -> dict[str, Any]:
        from_date, to_date = period_bounds(period, target)
        # 경계는 사용자 기준 날짜로 잡고, 조회는 UTC로 바꿔서 한다. `ended_at`이 UTC로
        # 저장되기도 하고, SQLite 드라이버는 바인딩된 값의 오프셋을 변환하지 않고 버리므로
        # 여기서 직접 맞춰두지 않으면 개발용 SQLite에서 하루치가 어긋난다.
        start_dt = datetime.combine(
            from_date, datetime.min.time(), tzinfo=REPORT_TIMEZONE
        ).astimezone(UTC)
        end_dt = datetime.combine(
            to_date + timedelta(days=1), datetime.min.time(), tzinfo=REPORT_TIMEZONE
        ).astimezone(UTC)
        calls = (
            await session.scalars(
                select(CallRecord).where(
                    CallRecord.parent_id == parent_id,
                    CallRecord.ended_at >= start_dt,
                    CallRecord.ended_at < end_dt,
                    CallRecord.state.in_(["ANALYZED", "ANALYSIS_EXCLUDED"]),
                )
            )
        ).all()
        existing = await session.scalar(
            select(Report)
            .where(
                Report.parent_id == parent_id,
                Report.period == period,
                Report.from_date == from_date,
                Report.to_date == to_date,
            )
            .order_by(Report.issued_at.desc())
            .limit(1)
        )
        latest_call_at = max(
            (aware_datetime(call.ended_at) for call in calls if call.ended_at), default=None
        )
        analyzed_ids = [call.id for call in calls if call.state == "ANALYZED"]
        # 응급 안내는 분석에서 빠진 짧은 통화에도 남는다(pipeline).
        notice_rows: list[HealthExtraction] = []
        if calls:
            notice_rows = list(
                await session.scalars(
                    select(HealthExtraction).where(
                        HealthExtraction.call_id.in_([call.id for call in calls]),
                        HealthExtraction.safety_notice.is_not(None),
                    )
                )
            )
        ended_at_by_call = {call.id: call.ended_at for call in calls}
        safety_notices = [
            {
                "callId": row.call_id,
                "observedAt": aware_datetime(ended_at_by_call[row.call_id]).isoformat(),
                "text": row.safety_notice,
            }
            for row in sorted(
                notice_rows,
                # 스냅샷과 비교하므로 같은 시각이어도 순서가 매번 같아야 한다.
                key=lambda row: (aware_datetime(ended_at_by_call[row.call_id]), row.call_id),
                reverse=True,
            )
        ]
        # 분석은 통화가 끝나고 몇 분 뒤에 끝난다. 그 사이에 만든 스냅샷은 종료 시각만 보면
        # 최신으로 보이므로, 분석된 통화 수나 응급 안내가 달라졌으면(지난 기간도) 다시 만든다.
        if (
            existing
            and existing.snapshot.get("analyzedCallCount") == len(analyzed_ids)
            and existing.snapshot.get("safetyNotices", []) == safety_notices
            and (
                to_date < date.today()
                or latest_call_at is None
                or latest_call_at <= aware_datetime(existing.issued_at)
            )
        ):
            # safetyNotices가 생기기 전에 만든 스냅샷도 같은 모양으로 준다.
            return {"safetyNotices": [], **existing.snapshot, "issuedAt": existing.issued_at}
        extractions = []
        features = []
        signals = []
        repeat_events = []
        if analyzed_ids:
            extractions = list(
                await session.scalars(
                    select(HealthExtraction).where(HealthExtraction.call_id.in_(analyzed_ids))
                )
            )
            features = list(
                await session.scalars(
                    select(AcousticFeature)
                    .where(
                        AcousticFeature.call_id.in_(analyzed_ids), AcousticFeature.status == "OK"
                    )
                    # 추이 그래프는 이 순서 그대로 선을 잇는다. 정렬하지 않으면 DB가 주는
                    # 순서를 따라가면서 선이 시간을 거슬러 꺾인다.
                    .order_by(AcousticFeature.observed_at)
                )
            )
            signals = list(
                await session.scalars(
                    select(ChangeSignal).where(ChangeSignal.call_id.in_(analyzed_ids))
                )
            )
            repeat_events = list(
                await session.scalars(
                    select(RepeatEvent).where(RepeatEvent.call_id.in_(analyzed_ids))
                )
            )
        # 기준선 행은 지표 × 시간대 × 종류로 항상 만들어지지만(signals.rebuild_baselines),
        # 그중에는 통화를 아무리 쌓아도 채워질 수 없는 것이 섞인다. 부모가 한 번도 통화하지
        # 않은 시간대, 그리고 측정 자체가 불가능한 지표(예: 감지기 미검증인 기침)가 그렇다.
        # 그런 행까지 "수집 중"으로 세면 리포트가 영영 READY가 되지 않으므로, 표본이 실제로
        # 들어오고 있는 기준선만 남은 수집 대상으로 본다.
        fillable = (
            select(AcousticFeature.metric, CallRecord.time_slot)
            .join(CallRecord, CallRecord.id == AcousticFeature.call_id)
            .where(
                CallRecord.parent_id == parent_id,
                AcousticFeature.status == "OK",
                AcousticFeature.value.is_not(None),
            )
            .distinct()
            .subquery()
        )
        collecting = await session.scalar(
            select(Baseline)
            .join(
                fillable,
                (Baseline.metric == fillable.c.metric)
                & (Baseline.time_slot == fillable.c.time_slot),
            )
            .where(Baseline.parent_id == parent_id, Baseline.status == "COLLECTING")
            .limit(1)
        )
        state = "EMPTY" if not calls else "BASELINE_COLLECTING" if collecting else "READY"
        conversation_items = {
            field: [value for row in extractions if (value := getattr(row, field))]
            for field in ("symptom", "medication", "activity", "sleep")
        }
        trends: dict[str, list[dict[str, Any]]] = {}
        for feature in features:
            trends.setdefault(feature.metric, []).append(
                {"date": feature.observed_at.date().isoformat(), "value": feature.value}
            )
        promoted = [signal_to_dict(item) for item in signals if item.promoted]
        acute = [signal_to_dict(item) for item in signals if item.acute]
        issued_at = datetime.now(UTC)
        snapshot = {
            "parentId": parent_id,
            "period": period,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "state": state,
            "emptyMessage": "이번 기간에 분석된 통화가 없어요" if state == "EMPTY" else None,
            "disclaimer": DISCLAIMER,
            "advisory": ADVISORY if promoted else None,
            "promotedSignals": promoted,
            "acuteSignals": acute,
            "conversationItems": conversation_items,
            "repeatObservation": {
                "count": len(repeat_events),
                "callsWithRepeat": len({item.call_id for item in repeat_events}),
                "label": f"되묻는 표현 {len(repeat_events)}회",
                "ruleVersion": repeat_events[0].rule_version if repeat_events else None,
            },
            "acousticTrends": [
                {"metric": metric, "points": points} for metric, points in trends.items()
            ],
            "analyzedCallCount": len(analyzed_ids),
            "safetyNotices": safety_notices,
        }
        session.add(
            Report(
                parent_id=parent_id,
                period=period,
                from_date=from_date,
                to_date=to_date,
                state=state,
                snapshot=snapshot,
                issued_at=issued_at,
            )
        )
        await session.commit()
        return {**snapshot, "issuedAt": issued_at}


def aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
