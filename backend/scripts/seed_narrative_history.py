"""정해진 서사대로 분석 결과를 DB에 직접 넣는다.

`seed_mock_history.py`는 음성 합성 → STT → Gemini → 음향 분석을 실제로 태우므로 값을
지정할 수 없고 외부 API 할당량에 걸린다. 화면을 확인하려는 목적이라면 그 단계가 필요 없다.
이 스크립트는 파이프라인을 건너뛰고 `acoustic_features`와 `health_extractions`를 그대로
채운 뒤, 기준선과 변화 신호만 실제 서비스 코드(`SignalService`)로 계산한다.

즉 "측정값을 어떻게 얻었는가"만 대체하고 "측정값으로 무엇을 판단하는가"는 손대지 않는다.
개발 환경 전용이며, 넣는 문장은 사람이 작성한 예시 대사다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.config import Settings
from app.container import AppContainer
from app.models import (
    AcousticFeature,
    CallRecord,
    CallState,
    ChangeSignal,
    HealthExtraction,
    Metric,
    User,
)

KST = ZoneInfo("Asia/Seoul")
# 넣은 통화를 나중에 골라내려고 room_name에 붙이는 표식. 다시 실행할 때 이 표식으로만
# 지우므로 실기기로 만든 통화는 건드리지 않는다.
ROOM_PREFIX = "narrative-"
SPEECH_RATE_UNIT = "음절/분"
CALL_HOUR_KST = 19  # AFTERNOON_EVENING 판정에 맞는 시각


class SeedError(RuntimeError):
    pass


def call_moment(week_offset: int, now_kst: datetime) -> datetime:
    """주차 오프셋을 실제 통화 시각으로 바꾼다.

    기준선은 calendar week 단위로 묶이므로(`signals.weekly_medians`) 각 통화가 서로 다른
    주에 떨어져야 한다. 같은 요일에서 주 단위로 빼면 그 조건이 유지된다. 아직 오지 않은
    시각은 하루 앞으로 당긴다 — 미래의 `ended_at`은 리포트 기간 조회에서 빠진다.
    """
    moment = datetime.combine(now_kst.date(), time(hour=CALL_HOUR_KST), tzinfo=KST)
    moment -= timedelta(weeks=abs(week_offset))
    if moment > now_kst:
        moment -= timedelta(days=1)
    return moment


async def purge_previous(session, parent_id: str) -> int:
    call_ids = list(
        await session.scalars(
            select(CallRecord.id).where(
                CallRecord.parent_id == parent_id,
                CallRecord.room_name.startswith(ROOM_PREFIX),
            )
        )
    )
    if not call_ids:
        return 0
    await session.execute(delete(AcousticFeature).where(AcousticFeature.call_id.in_(call_ids)))
    await session.execute(delete(HealthExtraction).where(HealthExtraction.call_id.in_(call_ids)))
    await session.execute(delete(ChangeSignal).where(ChangeSignal.call_id.in_(call_ids)))
    await session.execute(delete(CallRecord).where(CallRecord.id.in_(call_ids)))
    return len(call_ids)


def load_entries(path: str) -> list[dict]:
    # 동기 함수로 분리한다. async 안에서 pathlib을 직접 쓰면 이벤트 루프를 블로킹한다(ASYNC240).
    return json.loads(Path(path).read_text(encoding="utf-8"))


async def seed(args: argparse.Namespace) -> int:
    entries = load_entries(args.data)
    if not entries:
        raise SeedError(f"{args.data}에 항목이 없습니다")

    container = AppContainer(Settings())
    # 스키마는 백엔드 기동이 소유한다. 여기서는 맞는지 확인만 한다.
    await container.database.ensure_schema(auto_reset=False)

    now_kst = datetime.now(KST)
    async with container.database.sessions() as session:
        parent = await session.scalar(select(User).where(User.phone == args.parent_phone))
        child = await session.scalar(select(User).where(User.phone == args.child_phone))
        if parent is None or child is None:
            raise SeedError("부모 또는 자녀 계정이 없습니다. seed_demo_family.py를 먼저 실행하세요")

        removed = await purge_previous(session, parent.id)
        if removed:
            print(f"이전에 넣은 통화 {removed}건을 지웠습니다")

        created: list[tuple[CallRecord, dict]] = []
        seen_weeks: dict[str, int] = {}
        for entry in sorted(entries, key=lambda item: item["weekOffset"]):
            offset = int(entry["weekOffset"])
            moment = call_moment(offset, now_kst)
            week_key = (moment.date() - timedelta(days=moment.weekday())).isoformat()
            if week_key in seen_weeks:
                raise SeedError(
                    f"W{offset}과 W{seen_weeks[week_key]}가 같은 주({week_key})에 떨어집니다"
                )
            seen_weeks[week_key] = offset

            ended = moment.astimezone(UTC)
            started = ended - timedelta(minutes=args.duration_min)
            call = CallRecord(
                parent_id=parent.id,
                child_id=child.id,
                state=CallState.ANALYZED.value,
                room_name=f"{ROOM_PREFIX}w{abs(offset)}-{ended:%Y%m%d%H%M%S}",
                recording_enabled=True,
                asked_question_ids=list(entry.get("askedQuestionIds", [])),
                question_target_id=parent.id,
                started_at=started,
                accepted_at=started,
                ended_at=ended,
                duration_sec=args.duration_min * 60,
                time_slot=entry["timeSlot"],
                parent_speech_sec=args.parent_speech_sec,
                # 원본 오디오를 만든 적이 없으므로 폐기 시각을 지금으로 남긴다.
                raw_audio_purged_at=datetime.now(UTC),
            )
            session.add(call)
            await session.flush()

            session.add(
                AcousticFeature(
                    call_id=call.id,
                    subject_user_id=parent.id,
                    audio_source="DEVICE_RAW",
                    metric=Metric.SPEECH_RATE.value,
                    value=float(entry["speechRate"]),
                    unit=SPEECH_RATE_UNIT,
                    status="OK",
                    observed_at=ended,
                )
            )
            report = entry.get("selfReport", {})
            session.add(
                HealthExtraction(
                    call_id=call.id,
                    subject_user_id=parent.id,
                    parse_status="OK",
                    symptom=report.get("symptom"),
                    medication=report.get("medication"),
                    activity=report.get("activity"),
                    sleep=report.get("sleep"),
                )
            )
            created.append((call, entry))

        await session.flush()

        # 신호는 통화가 쌓인 순서대로 계산해야 "몇 주 연속"이 제대로 잡힌다.
        for call, _ in created:
            await container.signals.process_call(session, call)
        # process_call은 자기 자신을 뺀 기준선을 남기므로 마지막에 전체로 다시 세운다.
        await container.signals.rebuild_baselines(session, parent.id)
        await session.commit()

    for call, entry in created:
        local = call.ended_at.astimezone(KST)
        print(
            f"  W{entry['weekOffset']:>3}  {local:%Y-%m-%d(%a) %H:%M}  "
            f"{entry['timeSlot']:<18} {entry['speechRate']}{SPEECH_RATE_UNIT}"
        )
    print(f"\n통화 {len(created)}건을 넣었습니다. 리포트 스냅샷은 다음 조회 때 다시 만들어집니다.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="정해진 서사대로 분석 결과를 DB에 직접 넣는다 (개발 전용)"
    )
    parser.add_argument(
        "--data",
        default=str(Path(__file__).parent / "data" / "narrative_history.json"),
        help="주차별 측정값과 자기보고가 담긴 JSON",
    )
    parser.add_argument("--parent-phone", default="01000000010")
    parser.add_argument("--child-phone", default="01000000002")
    parser.add_argument("--duration-min", type=int, default=3)
    parser.add_argument("--parent-speech-sec", type=int, default=95)
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(seed(args)))
    except SeedError as error:
        print(f"실패: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
