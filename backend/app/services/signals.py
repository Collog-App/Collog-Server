from __future__ import annotations

import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    AcousticFeature,
    AnalysisMode,
    Baseline,
    BaselineKind,
    CallRecord,
    ChangeSignal,
    Metric,
)
from app.services.domain import subject_owns

METRIC_LABELS = {
    Metric.COUGH_EVENTS.value: "기침 횟수",
    Metric.SPEECH_RATE.value: "발화 속도",
    Metric.PAUSE_RATIO.value: "휴지 비율",
    Metric.F0_VARIATION.value: "기본주파수 변동",
}


def median_and_mad(values: list[float]) -> tuple[float, float]:
    center = float(statistics.median(values))
    raw_mad = float(statistics.median(abs(value - center) for value in values))
    return center, raw_mad


def compare(value: float, center: float, mad: float, threshold: float) -> dict[str, Any]:
    if mad <= 0:
        raise ValueError("MAD가 0인 기준선은 robust Z를 계산할 수 없습니다")
    if center == 0:
        delta_pct = 0.0 if value == 0 else value * 100.0
    else:
        delta_pct = ((value - center) / abs(center)) * 100
    robust_z = 0.6745 * (value - center) / mad
    significant = abs(robust_z) > threshold
    direction = "STABLE"
    if significant:
        direction = "UP" if value > center else "DOWN"
    return {
        "deltaPct": round(delta_pct, 1),
        "direction": direction,
        "robustZ": round(robust_z, 2),
        "significant": significant,
    }


class SignalService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def rebuild_baselines(
        self, session: AsyncSession, parent_id: str, exclude_call_id: str | None = None
    ) -> None:
        old_baselines = list(
            await session.scalars(select(Baseline).where(Baseline.parent_id == parent_id))
        )
        old_anchor_dates = {
            (item.metric, item.time_slot): item.anchor_set_at
            for item in old_baselines
            if item.kind == BaselineKind.ANCHOR.value and item.anchor_set_at
        }
        await session.execute(delete(Baseline).where(Baseline.parent_id == parent_id))
        today = datetime.now(UTC).date()
        this_week = week_start(today)
        rolling_from = this_week - timedelta(weeks=self.settings.baseline_window_weeks - 1)
        for metric in Metric:
            for time_slot in ("MORNING", "AFTERNOON_EVENING"):
                statement = (
                    select(AcousticFeature.value, AcousticFeature.observed_at)
                    .join(CallRecord, CallRecord.id == AcousticFeature.call_id)
                    .where(
                        subject_owns(AcousticFeature, parent_id),
                        CallRecord.time_slot == time_slot,
                        AcousticFeature.metric == metric.value,
                        AcousticFeature.status == "OK",
                        AcousticFeature.value.is_not(None),
                    )
                    .order_by(AcousticFeature.observed_at)
                )
                if exclude_call_id:
                    statement = statement.where(CallRecord.id != exclude_call_id)
                records = (await session.execute(statement)).all()
                weekly = weekly_medians(records)
                for kind in BaselineKind:
                    selected = weekly
                    if kind == BaselineKind.ANCHOR and weekly:
                        first = weekly[0][0]
                        selected = [
                            row
                            for row in weekly
                            if row[0] < first + timedelta(weeks=self.settings.baseline_window_weeks)
                        ]
                        window_from = first
                        window_to = first + timedelta(
                            weeks=self.settings.baseline_window_weeks, days=-1
                        )
                    elif kind == BaselineKind.ROLLING:
                        selected = [row for row in weekly if row[0] >= rolling_from]
                        window_from = rolling_from
                        window_to = this_week + timedelta(days=6)
                    else:
                        window_from = today
                        window_to = today
                    values = [float(value) for _, value in selected]
                    ready = len(values) >= self.settings.baseline_required_samples
                    center, mad = median_and_mad(values) if ready else (None, None)
                    status = (
                        "UNSCORABLE" if ready and mad == 0 else "READY" if ready else "COLLECTING"
                    )
                    anchor_key = (metric.value, time_slot)
                    session.add(
                        Baseline(
                            parent_id=parent_id,
                            metric=metric.value,
                            time_slot=time_slot,
                            kind=kind.value,
                            status=status,
                            sample_count=len(values),
                            required_count=self.settings.baseline_required_samples,
                            median=center,
                            mad=mad,
                            window_from=window_from,
                            window_to=window_to,
                            anchor_set_at=(
                                old_anchor_dates.get(anchor_key) or datetime.now(UTC)
                                if ready and kind == BaselineKind.ANCHOR
                                else None
                            ),
                        )
                    )
        await session.flush()

    async def process_call(
        self, session: AsyncSession, call: CallRecord, subject_id: str | None = None
    ) -> None:
        # BOTH 통화는 참여자마다 한 번씩 부른다. 비우면 S3 이전처럼 parent_id 다.
        subject_id = subject_id or call.parent_id
        # The current measurement must never be part of the baseline it is compared with.
        await self.rebuild_baselines(session, subject_id, exclude_call_id=call.id)
        statement = select(AcousticFeature).where(
            AcousticFeature.call_id == call.id,
            AcousticFeature.status == "OK",
            AcousticFeature.value.is_not(None),
        )
        if call.analysis_mode == AnalysisMode.BOTH.value:
            statement = statement.where(AcousticFeature.subject_user_id == subject_id)
        features = (await session.scalars(statement)).all()
        if not features or not call.time_slot:
            return
        for feature in features:
            baselines = (
                await session.scalars(
                    select(Baseline).where(
                        Baseline.parent_id == subject_id,
                        Baseline.metric == feature.metric,
                        Baseline.time_slot == call.time_slot,
                    )
                )
            ).all()
            by_kind = {item.kind: item for item in baselines}
            anchor = by_kind.get(BaselineKind.ANCHOR.value)
            rolling = by_kind.get(BaselineKind.ROLLING.value)
            vs_anchor = (
                compare(
                    float(feature.value),
                    float(anchor.median),
                    float(anchor.mad),
                    self.settings.robust_z_threshold,
                )
                if anchor and anchor.status == "READY" and anchor.mad and anchor.mad > 0
                else None
            )
            vs_rolling = (
                compare(
                    float(feature.value),
                    float(rolling.median),
                    float(rolling.mad),
                    self.settings.robust_z_threshold,
                )
                if rolling and rolling.status == "READY" and rolling.mad and rolling.mad > 0
                else None
            )
            consecutive = await self._consecutive_count(
                session,
                subject_id,
                feature.metric,
                call.time_slot,
                vs_anchor,
                feature.observed_at,
            )
            promoted = consecutive >= self.settings.promoted_consecutive_weeks
            acute = bool(vs_rolling and vs_rolling["significant"])
            label = METRIC_LABELS[feature.metric]
            session.add(
                ChangeSignal(
                    parent_id=subject_id,
                    call_id=call.id,
                    metric=feature.metric,
                    time_slot=call.time_slot,
                    vs_anchor=vs_anchor,
                    vs_rolling=vs_rolling,
                    consecutive_weeks=consecutive,
                    promoted=promoted,
                    acute=acute,
                    summary_text=(
                        f"{label} {consecutive}주 연속 변화, 처음 대비 "
                        f"{vs_anchor['deltaPct']:+.0f}%"
                        if promoted and vs_anchor
                        else None
                    ),
                    acute_text=(
                        f"{label}, 지난달 대비 {vs_rolling['deltaPct']:+.0f}%"
                        if acute and vs_rolling
                        else None
                    ),
                    observed_at=feature.observed_at,
                )
            )
        await session.flush()
        # Persist the new measurement into the baseline only after its signal was computed.
        await self.rebuild_baselines(session, subject_id)

    async def _consecutive_count(
        self,
        session: AsyncSession,
        parent_id: str,
        metric: str,
        time_slot: str,
        comparison: dict[str, Any] | None,
        observed_at: datetime,
    ) -> int:
        if not comparison or not comparison["significant"]:
            return 0
        previous_items = list(
            await session.scalars(
                select(ChangeSignal)
                .where(
                    ChangeSignal.parent_id == parent_id,
                    ChangeSignal.metric == metric,
                    ChangeSignal.time_slot == time_slot,
                    ChangeSignal.vs_anchor.is_not(None),
                )
                .order_by(ChangeSignal.observed_at.desc())
            )
        )
        return consecutive_calendar_weeks(
            previous_items, observed_at.date(), str(comparison["direction"])
        )


def week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def weekly_medians(records: list[Any]) -> list[tuple[date, float]]:
    grouped: dict[date, list[float]] = {}
    for row in records:
        observed = row.observed_at.date()
        grouped.setdefault(week_start(observed), []).append(float(row.value))
    return sorted((week, float(statistics.median(values))) for week, values in grouped.items())


def consecutive_calendar_weeks(
    previous_items: list[Any], current_date: date, direction: str
) -> int:
    current_week = week_start(current_date)
    by_week: dict[date, Any] = {}
    for item in previous_items:
        item_week = week_start(item.observed_at.date())
        if item_week < current_week:
            by_week.setdefault(item_week, item)
    count = 1
    expected_week = current_week - timedelta(weeks=1)
    while previous := by_week.get(expected_week):
        previous_comparison = previous.vs_anchor or {}
        if (
            not previous_comparison.get("significant")
            or previous_comparison.get("direction") != direction
        ):
            break
        count += 1
        expected_week -= timedelta(weeks=1)
    return count


def baseline_to_dict(item: Baseline) -> dict[str, Any]:
    now = date.today()
    anchor_age = None
    if item.anchor_set_at:
        anchor_age = max(0, (now - item.anchor_set_at.date()).days // 7)
    return {
        "parentId": item.parent_id,
        "metric": item.metric,
        "timeSlot": item.time_slot,
        "kind": item.kind,
        "status": item.status,
        "sampleCount": item.sample_count,
        "requiredCount": item.required_count,
        "remainingCalls": max(0, item.required_count - item.sample_count)
        if item.status == "COLLECTING"
        else None,
        "median": item.median,
        "mad": item.mad,
        "windowFrom": item.window_from,
        "windowTo": item.window_to,
        "anchorSetAt": item.anchor_set_at,
        "anchorAgeWeeks": anchor_age,
        "computedAt": item.computed_at,
    }


def signal_to_dict(item: ChangeSignal) -> dict[str, Any]:
    return {
        "signalId": item.id,
        "metric": item.metric,
        "timeSlot": item.time_slot,
        "vsAnchor": item.vs_anchor,
        "vsRolling": item.vs_rolling,
        "consecutiveWeeks": item.consecutive_weeks,
        "promoted": item.promoted,
        "acute": item.acute,
        "summaryText": item.summary_text,
        "acuteText": item.acute_text,
        # 이 dict는 API 응답이면서 동시에 Report.snapshot(JSON 컬럼)에 그대로 저장된다.
        # datetime을 그대로 두면 json.dumps가 스냅샷 INSERT에서 터지므로 문자열로 낸다.
        "observedAt": item.observed_at.isoformat(),
    }
