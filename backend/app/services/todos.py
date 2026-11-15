from __future__ import annotations

import hashlib
from datetime import date

# 추천 활동은 외부 AI 없이 정해 둔 목록에서 고른다(동의서 v5 범위 안). 일반적인 생활 습관만 두고
# 진단·약 복용 지시처럼 의료 판단이 들어가는 문장은 넣지 않는다(가드레일과 같은 경계).
SUGGESTION_POOL: dict[str, list[tuple[str, str]]] = {
    "FORGETFULNESS": [
        ("forgetfulness-diary", "오늘 있었던 일 한 줄 적기"),
        ("forgetfulness-call", "가족이나 친구와 안부 나누기"),
    ],
    "RESPIRATORY": [
        ("respiratory-water", "따뜻한 물 자주 마시기"),
        ("respiratory-air", "창문 열고 5분 환기하기"),
    ],
    "RHINITIS": [
        ("rhinitis-humidity", "실내 습도 확인하기"),
        ("rhinitis-wash", "외출 후 손 씻고 세수하기"),
    ],
    "DIABETES": [
        ("diabetes-walk", "식사 후 10분 걷기"),
        ("diabetes-meal", "끼니 거르지 않고 제때 먹기"),
    ],
    "HYPERTENSION": [
        ("hypertension-record", "혈압 재고 기록하기"),
        ("hypertension-soup", "국물은 조금만 먹기"),
    ],
    "DYSLIPIDEMIA": [
        ("dyslipidemia-veg", "채소 반찬 한 가지 더 먹기"),
        ("dyslipidemia-walk", "30분 걷기"),
    ],
    "ASTHMA": [
        ("asthma-dust", "외출 전 미세먼지 확인하기"),
        ("asthma-clean", "먼지 쌓인 곳 닦기"),
    ],
    "OBESITY": [
        ("obesity-stairs", "계단으로 한 층 오르기"),
        ("obesity-water", "물 6잔 마시기"),
    ],
}

DEFAULT_POOL: list[tuple[str, str]] = [
    ("default-walk", "20분 산책하기"),
    ("default-water", "물 6잔 마시기"),
    ("default-stretch", "자기 전 5분 스트레칭"),
    ("default-sleep", "정해진 시간에 잠자리에 들기"),
]

# 건강 앱 하루 요약에서 최근 걸음 수가 이보다 적으면 걷기를 맨 앞에 둔다.
LOW_STEPS = 3000
LOW_STEPS_SUGGESTION = ("steps-more", "어제보다 1,000걸음 더 걷기")
DAILY_SUGGESTIONS = 3


def _rotated(items: list[tuple[str, str]], seed: bytes) -> list[tuple[str, str]]:
    if not items:
        return []
    start = int.from_bytes(seed[:4], "big") % len(items)
    return items[start:] + items[:start]


def daily_suggestions(
    user_id: str,
    day: date,
    conditions: list[str],
    recent_steps: int | None,
) -> list[tuple[str, str]]:
    """그날의 추천 활동(key, 문장). 같은 사람·같은 날이면 같은 결과다."""
    seed = hashlib.sha256(f"{user_id}:{day.isoformat()}".encode()).digest()
    ordered: list[tuple[str, str]] = []
    if recent_steps is not None and recent_steps < LOW_STEPS:
        ordered.append(LOW_STEPS_SUGGESTION)
    by_condition = [item for code in conditions for item in SUGGESTION_POOL.get(code, [])]
    ordered += _rotated(by_condition, seed)
    ordered += _rotated(DEFAULT_POOL, seed[4:])

    chosen: list[tuple[str, str]] = []
    seen_text: set[str] = set()
    for key, text in ordered:
        if text in seen_text:
            continue
        seen_text.add(text)
        chosen.append((key, text))
        if len(chosen) == DAILY_SUGGESTIONS:
            break
    return chosen
