"""통화 한 건에 쓴 외부 AI 사용량(calls.ai_usage).

공급자 응답이 알려 준 값만 더한다. 응답에 값이 없으면 그 칸을 None(모름)으로 두고 추정하지
않는다. 칸이 아예 없으면 그 통화에서 그 공급자를 부르지 않았다는 뜻이다.

| 칸 | 출처 |
|---|---|
| sttAudioSec | Deepgram 응답 metadata.duration(트랙마다, 초) |
| geminiInputTokens | Gemini 응답 usageMetadata.promptTokenCount |
| geminiOutputTokens | candidatesTokenCount + thoughtsTokenCount(생각 토큰 포함) |
| geminiThoughtTokens | thoughtsTokenCount |
| ttsCharacters | ElevenLabs 음성 응답 헤더 character-cost(옛 문서 이름 x-character-count) |

분석은 전사·추출·요약을 동시에 도는 작업(asyncio task)으로 나눠 부르므로 계량기를 ContextVar 로
넘긴다. 새 작업은 만들 때의 문맥을 복사하므로 같은 계량기 객체에 더한다. 계량 중이 아니면
(스크립트·공급자 점검 등) 아무것도 남기지 않는다. 기록 실패가 분석·통화를 막지 않도록 여기서는
예외를 내지 않는다.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

Usage = dict[str, float | None]

USAGE_KEYS = (
    "sttAudioSec",
    "geminiInputTokens",
    "geminiOutputTokens",
    "geminiThoughtTokens",
    "ttsCharacters",
)

_meter: ContextVar[Usage | None] = ContextVar("ai_usage_meter", default=None)


@contextmanager
def metering() -> Iterator[Usage]:
    """이 안에서 부른 공급자 사용량을 모은 dict 를 돌려준다."""
    spent: Usage = {}
    token = _meter.set(spent)
    try:
        yield spent
    finally:
        _meter.reset(token)


# 한 값의 상한. 통화 한 건에 이보다 큰 사용량은 없으므로 넘으면 값이 없는 것으로 본다(아주 큰
# 정수가 들어와 합·평균 계산이 넘치지 않게).
MAX_VALUE = 1e9
GEMINI_KEYS = ("geminiInputTokens", "geminiOutputTokens", "geminiThoughtTokens")


def number(value: Any) -> float | None:
    """0 이상 MAX_VALUE 이하의 유한한 수만 값으로 본다. 어떤 입력에도 예외를 내지 않는다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        # 아주 큰 정수는 float 로 바꾸면 넘치므로 범위부터 본다(NaN 은 비교가 모두 거짓).
        if not 0 <= value <= MAX_VALUE or not math.isfinite(value):
            return None
    except (OverflowError, TypeError, ValueError):
        return None
    return value


def merge(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> Usage:
    """같은 칸은 더한다. 어느 한쪽이라도 None(모름)이거나 합이 상한을 넘으면 그 칸은 None 이다."""
    merged: Usage = (
        {key: number(value) for key, value in old.items()} if isinstance(old, Mapping) else {}
    )
    for key, value in new.items():
        value = number(value)
        if key not in merged:
            merged[key] = value
        elif merged[key] is None or value is None:
            merged[key] = None
        else:
            total = number(merged[key] + value)
            merged[key] = None if total is None else round(total, 3)
    return merged


def add(key: str, value: Any) -> None:
    spent = _meter.get()
    if spent is None:
        return
    try:
        spent.update(merge(spent, {key: value}))
    except Exception:
        logger.warning("AI usage could not be recorded (%s)", key, exc_info=True)


def gemini_values(usage_metadata: Any) -> dict[str, float | None]:
    if not isinstance(usage_metadata, Mapping):
        return dict.fromkeys(GEMINI_KEYS)
    candidates = number(usage_metadata.get("candidatesTokenCount", 0))
    thoughts = number(usage_metadata.get("thoughtsTokenCount", 0))
    return {
        "geminiInputTokens": number(usage_metadata.get("promptTokenCount")),
        "geminiOutputTokens": (
            None if candidates is None or thoughts is None else candidates + thoughts
        ),
        "geminiThoughtTokens": thoughts,
    }


def add_gemini(usage_metadata: Any) -> None:
    """Gemini 응답의 usageMetadata. 값이 0 인 칸은 응답 JSON 에서 빠지므로 없으면 0 으로 본다."""
    try:
        values = gemini_values(usage_metadata)
    except Exception:
        logger.warning("Gemini usage could not be read", exc_info=True)
        values = dict.fromkeys(GEMINI_KEYS)
    for key, value in values.items():
        add(key, value)
