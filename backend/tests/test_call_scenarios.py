from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from scripts.run_call_scenarios import run_scenarios
from scripts.scenario_audio import (
    SAMPLE_RATE,
    assemble_tracks,
    build_scenario_audio,
    load_scenarios,
    validate_scenarios,
)


def test_scenario_file_is_valid_and_covers_the_agreed_cases() -> None:
    scenarios = load_scenarios()
    assert validate_scenarios(scenarios) == []
    assert 10 <= len(scenarios) <= 12
    parent_text = " ".join(
        line["text"] for item in scenarios for line in item["lines"] if line["speaker"] == "PARENT"
    )
    for phrase in (
        "혈압약을 좀 줄였어",
        "가슴이 아프진 않아",
        "가슴이 너무 아파",
        "계단 오르니 숨이 찼어",
        "가만히 있어도 숨이 차",
    ):
        assert phrase in parent_text
    states = {item["id"]: item["expected"]["state"] for item in scenarios}
    assert states["short-call"] == states["short-call-emergency"] == "EXCLUDED"


def test_validate_scenarios_reports_broken_entries() -> None:
    errors = validate_scenarios(
        [
            {
                "id": "broken",
                "description": "x",
                "lines": [
                    {
                        "speaker": "CHILD",
                        "text": "기침하세요?",
                        "fakeFacts": {"symptom": "PRESENT"},
                    },
                    {"speaker": "PARENT", "text": "응"},
                ],
                "expected": {"state": "ANALYZED", "facts": {"mood": "PRESENT"}},
            }
        ]
    )
    assert any("fakeFacts는 PARENT" in item for item in errors)
    assert any("mood" in item for item in errors)
    assert any("safetyNotice" in item for item in errors)
    assert any("부모 대사" in item for item in errors)


def pcm(value: int, samples: int) -> bytes:
    return value.to_bytes(2, "little", signed=True) * samples


def test_tracks_keep_the_other_speaker_silent() -> None:
    lines = [
        {"speaker": "PARENT", "text": "a", "pauseBefore": 0},
        {"speaker": "CHILD", "text": "b"},
        {"speaker": "PARENT", "text": "c", "pauseBefore": 0.1},
    ]
    parent, child, spans = assemble_tracks(lines, [pcm(1000, 100), pcm(-1000, 50), pcm(7, 20)])

    total = 100 + round(0.4 * SAMPLE_RATE) + 50 + round(0.1 * SAMPLE_RATE) + 20
    assert len(parent) == len(child) == total * 2
    assert [(span.start, span.end) for span in spans] == [
        (0, 100),
        (6_500, 6_550),
        (8_150, 8_170),
    ]

    def samples(track: bytes, start: int, end: int) -> set[int]:
        view = memoryview(track).cast("h")
        return set(view[start:end])

    assert samples(parent, 0, 100) == {1000}
    assert samples(child, 0, 100) == {0}
    assert samples(parent, 100, 6_500) == samples(child, 100, 6_500) == {0}
    assert samples(child, 6_500, 6_550) == {-1000}
    assert samples(parent, 6_500, 6_550) == {0}
    assert samples(parent, 8_150, 8_170) == {7}
    assert samples(child, 8_150, 8_170) == {0}


async def test_scenario_audio_calls_elevenlabs_once_per_line(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=pcm(300, SAMPLE_RATE // 2))

    scenario = {
        "id": "demo",
        "lines": [
            {"speaker": "CHILD", "text": "잘 지내셨어요?"},
            {"speaker": "PARENT", "text": "응 잘 지냈어"},
            {"speaker": "PARENT", "text": "응 잘 지냈어"},
        ],
    }
    voices = {"PARENT": "voice-parent", "CHILD": "voice-child"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        audio = await build_scenario_audio(
            scenario, client=client, cache_dir=tmp_path, api_key="test-key", voices=voices
        )
        again = await build_scenario_audio(
            scenario, client=client, cache_dir=tmp_path, api_key="", voices=voices
        )

    # 같은 목소리·문장은 캐시를 쓰므로 두 번째 문장과 두 번째 빌드는 부르지 않는다.
    assert len(requests) == 2
    assert [request.url.path for request in requests] == [
        "/v1/text-to-speech/voice-child",
        "/v1/text-to-speech/voice-parent",
    ]
    first = requests[0]
    assert first.url.params["output_format"] == "pcm_16000"
    assert first.headers["xi-api-key"] == "test-key"
    # eleven_multilingual_v2는 language_code를 받지 않는다.
    assert json.loads(first.content) == {
        "text": "잘 지내셨어요?",
        "model_id": "eleven_multilingual_v2",
    }

    assert again == audio
    frames = []
    for path in (audio.parent, audio.child, audio.device_raw):
        with wave.open(io.BytesIO(path.read_bytes()), "rb") as handle:
            assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (
                1,
                2,
                SAMPLE_RATE,
            )
            frames.append(handle.getnframes())
    assert len(set(frames)) == 1
    assert frames[0] == round(3 * 0.4 * SAMPLE_RATE) + 3 * SAMPLE_RATE // 2
    assert audio.device_raw.read_bytes() == audio.parent.read_bytes()


# 기대값은 고치지 않고 지금 코드와 어긋난 곳을 여기 적어 둔다. 코드를 고치면 이 항목을 지운다.
KNOWN_FAKE_MODE_GAPS: dict[str, list[str]] = {}


async def test_fake_mode_runs_every_scenario_without_network(monkeypatch) -> None:
    def disallow_network(*args, **kwargs):
        pytest.fail("가짜 모드가 외부 요청을 만들었습니다")

    monkeypatch.setattr(httpx, "AsyncClient", disallow_network)
    outcomes = await run_scenarios(load_scenarios())

    failed = {
        outcome.scenario_id: [check.name for check in outcome.checks if not check.passed]
        for outcome in outcomes
        if not outcome.passed
    }
    assert failed == KNOWN_FAKE_MODE_GAPS
