from __future__ import annotations

import io
import json
import math
import wave
from array import array
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import Settings
from app.models import Metric
from app.schemas import ExtractionFact, GeminiExtractionResponse
from app.services.acoustics import (
    AcousticAnalysisInput,
    AcousticMeasurement,
    AudioDecodeError,
    CollogAcousticAnalyzer,
    decode_pcm_wav,
)
from app.services.gemini import ExtractionError, validate_facts
from app.services.repeat_detector import detect_repeat_events, repeat_rate_per_minute
from app.services.signals import (
    compare,
    consecutive_calendar_weeks,
    median_and_mad,
    weekly_medians,
)


def pcm_wav(samples: np.ndarray, sample_rate: int = 16_000) -> bytes:
    encoded = array("h", np.clip(samples * 32767, -32768, 32767).astype(np.int16)).tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(encoded)
    return output.getvalue()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("뭐라고?", True),
        ("다시 말해 줘", True),
        ("한 번 더 말해봐", True),
        ("잘 안 들려", True),
        ("못 들었어요", True),
        ("크게 말씀해", True),
        ("응?", True),
        ("어?", True),
        ("무슨 말이야?", True),
        ("어떻게?", True),
        ("내가 다시 말할게", False),
        ("어제 다시 갔어", False),
        ("응 맞아", False),
        ("어 그래 오늘 산책했어", False),
        ("한 번 더 먹었어", False),
        # 감탄사 규칙이 부분 일치로 증상 호소를 되묻기로 잡으면 안 된다.
        ("어지럽습니다", False),
        ("어지러워요", False),
        ("응급실에 갔어", False),
        ("어깨가 아파", False),
        ("어어?", True),
    ],
)
def test_repeat_detector_rules(text: str, expected: bool) -> None:
    events = detect_repeat_events(
        [{"speaker": "PARENT", "startMs": 100, "endMs": 800, "text": text}]
    )
    assert bool(events) is expected


def test_repeat_detector_uses_parent_and_merges_three_seconds() -> None:
    events = detect_repeat_events(
        [
            {"speaker": "CHILD", "startMs": 0, "endMs": 300, "text": "뭐라고?"},
            {"speaker": "PARENT", "startMs": 1_000, "endMs": 1_500, "text": "뭐라고?"},
            {"speaker": "PARENT", "startMs": 3_000, "endMs": 3_500, "text": "잘 안 들려"},
            {"speaker": "PARENT", "startMs": 8_000, "endMs": 8_500, "text": "크게 말해"},
        ]
    )
    assert len(events) == 2
    assert events[0].category == "HEARING_DIFFICULTY"
    assert repeat_rate_per_minute(2, 30) == 4


def test_gemini_semantic_validator_rejects_child_evidence() -> None:
    response = GeminiExtractionResponse(
        facts=[
            ExtractionFact(
                category="symptom",
                summary="열이 있음",
                polarity="PRESENT",
                evidence_segment_ids=["child-1"],
            )
        ]
    )
    with pytest.raises(ExtractionError, match="PARENT"):
        validate_facts(
            response,
            [
                {"id": "parent-1", "speaker": "PARENT", "text": "괜찮아"},
                {"id": "child-1", "speaker": "CHILD", "text": "열 있어요?"},
            ],
        )


def test_gemini_semantic_validator_drops_fact_from_generic_parent_reply() -> None:
    response = GeminiExtractionResponse(
        facts=[
            ExtractionFact(
                category="symptom",
                summary="무릎 통증은 없음",
                polarity="ABSENT",
                evidence_segment_ids=["parent-1"],
            ),
            ExtractionFact(
                category="activity",
                summary="공원을 걸음",
                polarity="PRESENT",
                evidence_segment_ids=["parent-2"],
            ),
        ]
    )
    result = validate_facts(
        response,
        [
            {"id": "parent-1", "speaker": "PARENT", "text": "아니 괜찮아"},
            {"id": "parent-2", "speaker": "PARENT", "text": "오늘 공원을 걸었어"},
        ],
    )
    assert result.symptom is None
    assert result.activity == "공원을 걸음"


def test_gemini_semantic_validator_drops_medical_advice() -> None:
    result = validate_facts(
        GeminiExtractionResponse(
            facts=[
                ExtractionFact(
                    category="symptom",
                    summary="기침이 있으니 병원에 가서 치료해야 함",
                    polarity="PRESENT",
                    evidence_segment_ids=["parent-1"],
                )
            ]
        ),
        [{"id": "parent-1", "speaker": "PARENT", "text": "오늘 기침을 했어"}],
    )
    assert result.facts == []


def timing_segments() -> list[dict]:
    segments = []
    for segment_index in range(4):
        offset = segment_index * 3_000
        words = [
            {
                "text": "가나다라",
                "startMs": offset + index * 1_000,
                "endMs": offset + index * 1_000 + 500,
                "confidence": 0.99,
            }
            for index in range(3)
        ]
        segments.append({"speaker": "PARENT", "words": words})
    return segments


@pytest.mark.parametrize("sample_rate", [16_000, 48_000])
async def test_acoustic_analyzer_computes_all_four_metrics(sample_rate: int) -> None:
    seconds = 6
    time = np.arange(sample_rate * seconds) / sample_rate
    samples = 0.2 * np.sin(2 * math.pi * 150 * time)
    analyzer = CollogAcousticAnalyzer(Settings(mock_external_services=True))
    results = await analyzer.analyze(
        AcousticAnalysisInput(
            audio=pcm_wav(samples, sample_rate),
            content_type="audio/wav",
            declared_sample_rate=sample_rate,
            source="DEVICE_RAW",
            parent_segments=timing_segments(),
            parent_speech_seconds=25,
        )
    )
    by_metric = {item.metric: item for item in results}
    assert set(by_metric) == set(Metric)
    assert by_metric[Metric.SPEECH_RATE].value == pytest.approx(480, rel=0.01)
    assert by_metric[Metric.PAUSE_RATIO].value == pytest.approx(40, abs=0.1)
    assert by_metric[Metric.F0_VARIATION].value is not None
    assert by_metric[Metric.F0_VARIATION].value < 0.2
    assert by_metric[Metric.SPEECH_RATE].status == "OK"
    assert by_metric[Metric.PAUSE_RATIO].status == "OK"
    assert by_metric[Metric.F0_VARIATION].status == "OK"
    # 기침은 검증 전까지 숫자를 만들지 않는다.
    assert by_metric[Metric.COUGH_EVENTS].status == "UNMEASURABLE"
    assert by_metric[Metric.COUGH_EVENTS].value is None


def transient_wav(sample_rate: int = 16_000) -> bytes:
    """배경음 위에 0.22초 광대역 폭발음 두 개를 심은 결정론적 fixture."""
    seconds = 8
    time = np.arange(sample_rate * seconds) / sample_rate
    samples = 0.03 * np.sin(2 * math.pi * 150 * time)
    random = np.random.default_rng(7)
    for start_seconds in (2.0, 5.0):
        start = round(start_seconds * sample_rate)
        end = start + round(0.22 * sample_rate)
        samples[start:end] += 0.75 * random.normal(size=end - start) * np.hanning(end - start)
    return pcm_wav(np.clip(samples, -0.95, 0.95), sample_rate)


async def analyze_transients(settings: Settings) -> AcousticMeasurement:
    results = await CollogAcousticAnalyzer(settings).analyze(
        AcousticAnalysisInput(
            audio=transient_wav(),
            content_type="audio/wav",
            declared_sample_rate=16_000,
            source="DEVICE_RAW",
            parent_segments=timing_segments(),
            parent_speech_seconds=25,
        )
    )
    return next(item for item in results if item.metric == Metric.COUGH_EVENTS)


async def test_cough_events_stay_unmeasurable_until_the_detector_is_validated() -> None:
    # transient-heuristic-v1은 실제 기침 녹음에서 재현율이 0에 가깝다. 합성 폭발음을 세는
    # 능력이 남아 있어도 `0.0 OK`가 기준선에 쌓이면 안 되므로 값 자체를 내보내지 않는다.
    cough = await analyze_transients(Settings(mock_external_services=True))
    assert cough.status == "UNMEASURABLE"
    assert cough.unmeasurable_reason == "DETECTOR_NOT_VALIDATED"
    assert cough.value is None


async def test_cough_candidate_detector_counts_separated_transients() -> None:
    # calibration harness가 회귀를 측정할 수 있도록 heuristic 계산 경로는 살아 있어야 한다.
    cough = await analyze_transients(
        Settings(
            mock_external_services=True,
            cough_detector_validated=True,
            cough_detector="transient-heuristic-v1",
        )
    )
    assert cough.status == "OK"
    assert cough.value == 2
    assert cough.unit == "회"


async def test_hear_detector_reports_missing_model_instead_of_guessing() -> None:
    # 모델 가중치는 HAI-DEF 약관 대상이라 저장소에 없다. 파일이 없을 때 조용히 0을 만들거나
    # 예외로 파이프라인을 끊지 않고 사유를 남겨야 한다.
    cough = await analyze_transients(
        Settings(
            mock_external_services=True,
            cough_detector_validated=True,
            cough_model_path=Path("models/does-not-exist.onnx"),
        )
    )
    assert cough.status == "UNMEASURABLE"
    assert cough.unmeasurable_reason == "MODEL_UNAVAILABLE"
    assert cough.unit == "구간"


async def test_hear_detector_rejects_a_model_with_an_unexpected_checksum(tmp_path: Path) -> None:
    impostor = tmp_path / "hear-event-detector-small.onnx"
    impostor.write_bytes(b"not the model")
    cough = await analyze_transients(
        Settings(
            mock_external_services=True,
            cough_detector_validated=True,
            cough_model_path=impostor,
        )
    )
    assert cough.status == "UNMEASURABLE"
    assert cough.unmeasurable_reason == "MODEL_CHECKSUM_MISMATCH"


def test_pcm_quality_failures_are_explicit() -> None:
    with pytest.raises(AudioDecodeError, match="INVALID_AUDIO"):
        decode_pcm_wav(b"not-wave", 16_000)
    time = np.arange(16_000 * 6) / 16_000
    wav = pcm_wav(0.1 * np.sin(2 * math.pi * 120 * time), 16_000)
    with pytest.raises(AudioDecodeError, match="SAMPLE_RATE_MISMATCH"):
        decode_pcm_wav(wav, 48_000)


def test_weekly_baseline_uses_one_median_per_calendar_week() -> None:
    monday = datetime(2026, 7, 6, tzinfo=UTC)
    rows = [
        SimpleNamespace(value=10, observed_at=monday),
        SimpleNamespace(value=30, observed_at=monday + timedelta(days=2)),
        SimpleNamespace(value=40, observed_at=monday + timedelta(weeks=1)),
    ]
    assert weekly_medians(rows) == [(date(2026, 7, 6), 20.0), (date(2026, 7, 13), 40.0)]
    assert median_and_mad([10, 10, 10, 10]) == (10, 0)
    with pytest.raises(ValueError, match="MAD"):
        compare(11, 10, 0, 1.5)


def test_consecutive_signal_requires_distinct_unbroken_weeks() -> None:
    direction = {"significant": True, "direction": "DOWN"}
    items = [
        SimpleNamespace(observed_at=datetime(2026, 7, 27, tzinfo=UTC), vs_anchor=direction),
        # Same week must not add another count.
        SimpleNamespace(observed_at=datetime(2026, 7, 28, tzinfo=UTC), vs_anchor=direction),
        SimpleNamespace(observed_at=datetime(2026, 7, 20, tzinfo=UTC), vs_anchor=direction),
        SimpleNamespace(observed_at=datetime(2026, 7, 13, tzinfo=UTC), vs_anchor=direction),
    ]
    assert consecutive_calendar_weeks(items, date(2026, 8, 3), "DOWN") == 4
    assert consecutive_calendar_weeks(items[2:], date(2026, 8, 3), "DOWN") == 1


def test_prompt_eval_suite_has_forty_safe_dummy_cases() -> None:
    path = Path(__file__).parents[1] / "evals" / "extraction_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    assert len(cases) >= 40
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(
        {item["speaker"] for item in case["segments"]} <= {"PARENT", "CHILD"} for case in cases
    )
