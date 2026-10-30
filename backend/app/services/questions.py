from __future__ import annotations

import hashlib
from datetime import date

from app.config import Settings
from app.schemas import QuestionView

QUESTION_POOL: dict[str, list[tuple[str, str]]] = {
    # Open questions (docs/profile-question-report-design.md §5): yes/no answers leave no record.
    "FORGETFULNESS": [
        ("forgetfulness-day", "오늘은 어떤 일을 하며 지내셨어요?"),
        ("forgetfulness-med", "오늘 약은 언제 챙겨 드셨어요?"),
        ("forgetfulness-recall", "어제 저녁에는 뭐 드셨어요?"),
    ],
    "RESPIRATORY": [
        ("respiratory-breath", "오늘 숨쉬기는 좀 어떠셨어요?"),
        ("respiratory-cough", "요즘 기침이나 가래는 어떠세요?"),
    ],
    "RHINITIS": [
        ("rhinitis-nose", "요즘 코 상태는 어떠세요?"),
        ("rhinitis-sleep", "코가 불편할 때 밤에는 어떻게 주무세요?"),
    ],
    "DIABETES": [
        ("diabetes-walk", "오늘 산책이나 가벼운 운동 하셨어요?"),
        ("diabetes-meal", "오늘 식사는 제때 챙겨 드셨어요?"),
        ("diabetes-dizzy", "오늘 어지럽거나 기운 빠진 적은 없으셨어요?"),
    ],
    "HYPERTENSION": [
        ("hypertension-med", "오늘 혈압약은 챙겨 드셨어요?"),
        ("hypertension-head", "오늘 머리가 아프거나 어지럽진 않으셨어요?"),
    ],
    "DYSLIPIDEMIA": [
        ("dyslipidemia-med", "오늘 약은 잊지 않고 드셨어요?"),
        ("dyslipidemia-activity", "오늘은 얼마나 움직이셨어요?"),
    ],
    "ASTHMA": [
        ("asthma-night-cough", "어젯밤 기침 때문에 깨셨어요?"),
        ("asthma-breath", "오늘 숨이 차거나 답답한 적은 없었어요?"),
    ],
    "OBESITY": [
        ("obesity-walk", "오늘 밖에 나가 걸으셨어요?"),
        ("obesity-meal", "오늘 식사는 평소와 비슷하게 하셨어요?"),
    ],
}

DEFAULT_POOL = [
    ("default-sleep", "요즘 수면은 어떠세요?"),
    ("default-meal", "오늘 식사는 잘 하셨어요?"),
    ("default-activity", "오늘은 몸을 좀 움직이셨어요?"),
]


def daily_questions(
    settings: Settings,
    conditions: list[str],
    excluded_ids: set[str],
    parent_id: str,
) -> tuple[str, list[QuestionView]]:
    candidates: list[tuple[str, str, str | None]] = []
    for condition in conditions:
        candidates.extend(
            (question_id, text, condition) for question_id, text in QUESTION_POOL[condition]
        )
    source = "CONDITION_POOL" if candidates else "DEFAULT_POOL"
    if not candidates:
        candidates = [(question_id, text, None) for question_id, text in DEFAULT_POOL]
    available = [item for item in candidates if item[0] not in excluded_ids] or candidates
    seed = hashlib.sha256(f"{parent_id}:{date.today().isoformat()}".encode()).digest()
    start = int.from_bytes(seed[:4], "big") % len(available)
    chosen = (available[start:] + available[:start])[: min(2, len(available))]
    views = []
    for question_id, text, condition in chosen:
        tts_url = None
        duration_ms = min(5000, max(2200, len(text.replace(" ", "")) * 180))
        views.append(
            QuestionView(
                question_id=question_id,
                text=text,
                condition_code=condition,
                tts_asset_url=tts_url,
                duration_ms=duration_ms,
                tts_mode="IOS_LOCAL",
            )
        )
    return source, views
