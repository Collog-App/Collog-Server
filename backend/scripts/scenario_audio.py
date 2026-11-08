"""통화 시나리오(evals/call_scenarios.json)의 대사로 화자별 통화 음성을 만든다. 개발 전용.

문장마다 ElevenLabs TTS로 만든 음성을 캐시 폴더(기본 `backend/.scenario-audio/lines/`)에 두고,
같은 목소리·모델·문장이면 다시 만들지 않는다. 대화 순서대로 이어 붙여 한 통화당 세 파일을
만든다(`calls/<시나리오 id>/`).

- `parent.wav`, `child.wav`: Track Egress가 만들었을 화자별 트랙. 상대가 말하는 동안은 무음.
- `device_raw.wav`: 부모 기기가 올리는 분석용 녹음(DEVICE_RAW). 앱은 16 kHz로 녹음하고
  (`AudioConstraints.raw_capture_sample_rate`) 에코 제거로 자녀 목소리는 거의 빠지므로 부모
  트랙과 같은 내용이다.

셋 다 16 kHz mono 16-bit PCM WAV라서 `replay_call.audio_meta`가 그대로 받는다.

    # 새로 만들 문장 수와 글자 수만 본다(ElevenLabs를 부르지 않는다)
    uv run python -m scripts.scenario_audio --dry-run
    # 음성 만들기. 키는 ELEVENLABS_API_KEY 또는 맥 키체인 collog-elevenlabs-api-key
    COLLOG_TEST_VOICE_PARENT=<voice id> COLLOG_TEST_VOICE_CHILD=<voice id> \\
        uv run python -m scripts.scenario_audio --only cough

시나리오 파일 형식은 `validate_scenarios`가 검사한다. `fakeFacts`는 가짜 모드
(`run_call_scenarios.py`)에서 Gemini 대신 답할 값이고 실제 모드에서는 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import numpy as np

from app.config import Settings
from app.services.http import request_with_retry

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = BACKEND_DIR / "evals" / "call_scenarios.json"
DEFAULT_CACHE_DIR = BACKEND_DIR / ".scenario-audio"
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"
SAMPLE_RATE = 16_000
# ElevenLabs pcm_* 출력은 헤더 없는 mono S16LE다.
OUTPUT_FORMAT = f"pcm_{SAMPLE_RATE}"
DEFAULT_PAUSE_SECONDS = 0.4

SPEAKERS = {"PARENT", "CHILD"}
CATEGORIES = {"symptom", "medication", "activity", "sleep"}
POLARITIES = {"PRESENT", "ABSENT", "UNCERTAIN"}
STATES = {"ANALYZED", "EXCLUDED"}
# 부모 발화 20초 기준(parent_min_speech_seconds)에서 양쪽 모드가 같은 판정을 내도록 둔 여유.
# 실제 TTS 말 빠르기는 아직 재지 않았다. 생성 뒤 `parent_voiced_seconds` 경고로 확인한다.
MIN_ANALYZED_PARENT_CHARS = 200
MAX_EXCLUDED_PARENT_CHARS = 40


class ScenarioAudioError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Span:
    """한 대사가 통화 안에서 차지하는 구간. 단위는 16 kHz 샘플."""

    index: int
    speaker: str
    text: str
    start: int
    end: int

    @property
    def start_ms(self) -> int:
        return self.start * 1000 // SAMPLE_RATE

    @property
    def end_ms(self) -> int:
        return self.end * 1000 // SAMPLE_RATE


@dataclass(frozen=True, slots=True)
class ScenarioAudio:
    parent: Path
    child: Path
    device_raw: Path


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def parent_chars(scenario: dict[str, Any]) -> int:
    return sum(
        len(re.sub(r"\s", "", line["text"]))
        for line in scenario["lines"]
        if line["speaker"] == "PARENT"
    )


def validate_scenarios(scenarios: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    ids = [item.get("id") for item in scenarios]
    if len(set(ids)) != len(ids):
        errors.append("시나리오 id가 겹칩니다")
    for scenario in scenarios:
        sid = scenario.get("id") or "?"
        if not scenario.get("description"):
            errors.append(f"{sid}: description이 없습니다")
        lines = scenario.get("lines") or []
        if not lines:
            errors.append(f"{sid}: lines가 비었습니다")
        for number, line in enumerate(lines):
            where = f"{sid} lines[{number}]"
            if line.get("speaker") not in SPEAKERS:
                errors.append(f"{where}: speaker는 PARENT 또는 CHILD")
            if not str(line.get("text", "")).strip():
                errors.append(f"{where}: text가 비었습니다")
            pause = line.get("pauseBefore", DEFAULT_PAUSE_SECONDS)
            if not isinstance(pause, int | float) or pause < 0:
                errors.append(f"{where}: pauseBefore는 0 이상의 초")
            fake = line.get("fakeFacts", {})
            if fake and line.get("speaker") != "PARENT":
                errors.append(f"{where}: fakeFacts는 PARENT 대사에만 붙인다")
            for category, polarity in fake.items():
                if category not in CATEGORIES or polarity not in POLARITIES:
                    errors.append(f"{where}: fakeFacts {category}={polarity}")
        expected = scenario.get("expected") or {}
        if expected.get("state") not in STATES:
            errors.append(f"{sid}: expected.state는 ANALYZED 또는 EXCLUDED")
        for category, polarity in (expected.get("facts") or {}).items():
            if category not in CATEGORIES or polarity not in POLARITIES | {"ANY"}:
                errors.append(f"{sid}: expected.facts {category}={polarity}")
        for key in ("summaryIncludes", "summaryExcludes"):
            for pattern in expected.get(key, []):
                try:
                    re.compile(pattern)
                except re.error:
                    errors.append(f"{sid}: {key} 정규식 오류 {pattern!r}")
        if not isinstance(expected.get("safetyNotice"), bool):
            errors.append(f"{sid}: expected.safetyNotice는 true/false")
        chars = parent_chars(scenario) if lines else 0
        if expected.get("state") == "ANALYZED" and chars < MIN_ANALYZED_PARENT_CHARS:
            errors.append(f"{sid}: 분석될 통화의 부모 대사 {chars}자 < {MIN_ANALYZED_PARENT_CHARS}")
        if expected.get("state") == "EXCLUDED" and chars > MAX_EXCLUDED_PARENT_CHARS:
            errors.append(f"{sid}: 짧은 통화의 부모 대사 {chars}자 > {MAX_EXCLUDED_PARENT_CHARS}")
    return errors


def layout(lines: list[dict[str, Any]], lengths: list[int]) -> list[Span]:
    """대사를 순서대로 놓는다. 각 대사 앞에 pauseBefore만큼(기본 0.4초) 쉰다."""
    spans: list[Span] = []
    cursor = 0
    for index, (line, length) in enumerate(zip(lines, lengths, strict=True)):
        cursor += round(float(line.get("pauseBefore", DEFAULT_PAUSE_SECONDS)) * SAMPLE_RATE)
        spans.append(Span(index, line["speaker"], line["text"], cursor, cursor + length))
        cursor += length
    return spans


def assemble_tracks(
    lines: list[dict[str, Any]], clips: list[bytes]
) -> tuple[bytes, bytes, list[Span]]:
    """문장별 PCM(S16LE)을 화자 트랙 둘로 이어 붙인다. 두 트랙은 길이가 같다."""
    spans = layout(lines, [len(clip) // 2 for clip in clips])
    total = spans[-1].end if spans else 0
    tracks = {"PARENT": bytearray(total * 2), "CHILD": bytearray(total * 2)}
    for span, clip in zip(spans, clips, strict=True):
        tracks[span.speaker][span.start * 2 : span.end * 2] = clip[: (span.end - span.start) * 2]
    return bytes(tracks["PARENT"]), bytes(tracks["CHILD"]), spans


def to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def wav_frames(payload: bytes) -> bytes:
    with wave.open(io.BytesIO(payload), "rb") as handle:
        if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (
            1,
            2,
            SAMPLE_RATE,
        ):
            raise ScenarioAudioError("캐시 음성이 16 kHz mono 16-bit WAV가 아닙니다")
        return handle.readframes(handle.getnframes())


def voiced_seconds(pcm: bytes, threshold: int = 500) -> float:
    """20ms 창 중 소리가 있는 창만 센다. Deepgram 없이 부모 발화 길이를 어림한다."""
    window = SAMPLE_RATE // 50
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    frames = samples[: len(samples) // window * window].reshape(-1, window)
    return float((np.abs(frames).max(axis=1) >= threshold).sum() * window / SAMPLE_RATE)


def secret(env_name: str, keychain_service: str) -> str:
    """환경변수, 없으면 맥 키체인(-a collog)에서 읽는다. 값은 출력하지 않는다."""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "collog", "-s", keychain_service, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def line_cache_path(cache_dir: Path, voice_id: str, model: str, text: str) -> Path:
    identity = "\n".join((voice_id, model, OUTPUT_FORMAT, text))
    return cache_dir / "lines" / f"{hashlib.sha256(identity.encode()).hexdigest()[:20]}.wav"


def voice_for(speaker: str, voices: dict[str, str]) -> str:
    voice = voices.get(speaker, "")
    if not voice:
        raise ScenarioAudioError(f"{speaker} voice ID가 없습니다 (COLLOG_TEST_VOICE_{speaker})")
    return voice


def missing_lines(
    scenarios: list[dict[str, Any]], cache_dir: Path, voices: dict[str, str], model: str
) -> list[tuple[str, str]]:
    """캐시에 없는 (화자, 문장). 같은 문장은 한 번만 센다."""
    missing: dict[tuple[str, str], None] = {}
    for scenario in scenarios:
        for line in scenario["lines"]:
            # voice ID를 모르면 캐시를 찾을 수 없으니 새로 만들 것으로 센다.
            voice = voices.get(line["speaker"], "")
            if not voice or not line_cache_path(cache_dir, voice, model, line["text"]).exists():
                missing[(line["speaker"], line["text"])] = None
    return list(missing)


async def synthesize(
    client: httpx.AsyncClient,
    api_key: str,
    voice_id: str,
    model: str,
    text: str,
    base_url: str = ELEVENLABS_BASE_URL,
) -> bytes:
    body: dict[str, object] = {"text": text, "model_id": model}
    # 공식 문서상 language_code는 multilingual_v2 계열이 받지 않는다(app/services/tts.py와 같음).
    if model != "eleven_multilingual_v2":
        body["language_code"] = "ko"
    try:
        response = await request_with_retry(
            client,
            "POST",
            f"{base_url.rstrip('/')}/v1/text-to-speech/{quote(voice_id, safe='')}",
            params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": api_key},
            json=body,
        )
    except httpx.HTTPStatusError as exc:
        raise ScenarioAudioError(f"ElevenLabs TTS 실패 {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise ScenarioAudioError(f"ElevenLabs TTS 요청 실패: {type(exc).__name__}") from exc
    if len(response.content) < 2:
        raise ScenarioAudioError("ElevenLabs가 빈 오디오를 돌려줬습니다")
    return response.content[: len(response.content) // 2 * 2]


async def line_clip(
    client: httpx.AsyncClient,
    cache_dir: Path,
    api_key: str,
    voice_id: str,
    model: str,
    text: str,
) -> bytes:
    path = line_cache_path(cache_dir, voice_id, model, text)
    if await asyncio.to_thread(path.exists):
        return wav_frames(await asyncio.to_thread(path.read_bytes))
    if not api_key:
        raise ScenarioAudioError("ElevenLabs API 키가 없습니다 (ELEVENLABS_API_KEY)")
    pcm = await synthesize(client, api_key, voice_id, model, text)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, to_wav(pcm))
    return pcm


async def build_scenario_audio(
    scenario: dict[str, Any],
    *,
    client: httpx.AsyncClient,
    cache_dir: Path,
    api_key: str,
    voices: dict[str, str],
    model: str = DEFAULT_MODEL,
) -> ScenarioAudio:
    clips = [
        await line_clip(
            client, cache_dir, api_key, voice_for(line["speaker"], voices), model, line["text"]
        )
        for line in scenario["lines"]
    ]
    parent, child, _ = assemble_tracks(scenario["lines"], clips)
    out_dir = cache_dir / "calls" / scenario["id"]
    audio = ScenarioAudio(
        parent=out_dir / "parent.wav",
        child=out_dir / "child.wav",
        device_raw=out_dir / "device_raw.wav",
    )
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    for path, pcm in ((audio.parent, parent), (audio.child, child), (audio.device_raw, parent)):
        await asyncio.to_thread(path.write_bytes, to_wav(pcm))
    return audio


def parent_track_warning(scenario: dict[str, Any], audio: ScenarioAudio, min_seconds: int) -> str:
    seconds = voiced_seconds(wav_frames(audio.parent.read_bytes()))
    expected = scenario["expected"]["state"]
    # 기준과 가까우면 Deepgram 단어 시간 합이 어느 쪽으로 떨어질지 모른다.
    if expected == "ANALYZED" and seconds < min_seconds * 1.2:
        return f"부모 음성 {seconds:.1f}초: 분석 기준 {min_seconds}초에 가까워 제외될 수 있음"
    if expected == "EXCLUDED" and seconds > min_seconds * 0.8:
        return f"부모 음성 {seconds:.1f}초: 분석 기준 {min_seconds}초에 가까워 분석될 수 있음"
    return ""


def pick_scenarios(scenarios: list[dict[str, Any]], only: list[str] | None) -> list[dict[str, Any]]:
    if not only:
        return scenarios
    unknown = sorted(set(only) - {item["id"] for item in scenarios})
    if unknown:
        raise ScenarioAudioError(f"없는 시나리오: {', '.join(unknown)}")
    return [item for item in scenarios if item["id"] in only]


def voices_from(args: argparse.Namespace) -> dict[str, str]:
    return {
        "PARENT": args.parent_voice or os.environ.get("COLLOG_TEST_VOICE_PARENT", ""),
        "CHILD": args.child_voice or os.environ.get("COLLOG_TEST_VOICE_CHILD", ""),
    }


def add_audio_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--only", nargs="+", help="이 id의 시나리오만")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS_PATH)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--parent-voice", help="부모 voice ID (기본: COLLOG_TEST_VOICE_PARENT)")
    parser.add_argument("--child-voice", help="자녀 voice ID (기본: COLLOG_TEST_VOICE_CHILD)")
    parser.add_argument("--tts-model", default=DEFAULT_MODEL)


async def generate(
    scenarios: list[dict[str, Any]],
    *,
    cache_dir: Path,
    voices: dict[str, str],
    model: str,
    api_key: str,
    min_speech_seconds: int = 20,
) -> dict[str, ScenarioAudio]:
    built: dict[str, ScenarioAudio] = {}
    async with httpx.AsyncClient(timeout=120.0) as client:
        for scenario in scenarios:
            audio = await build_scenario_audio(
                scenario,
                client=client,
                cache_dir=cache_dir,
                api_key=api_key,
                voices=voices,
                model=model,
            )
            built[scenario["id"]] = audio
            warning = await asyncio.to_thread(
                parent_track_warning, scenario, audio, min_speech_seconds
            )
            print(f"  {scenario['id']}: {audio.parent.parent}")
            if warning:
                print(f"    경고: {warning}")
    return built


def main() -> None:
    parser = argparse.ArgumentParser(description="통화 시나리오 대사로 화자별 음성을 만든다")
    add_audio_arguments(parser)
    parser.add_argument("--dry-run", action="store_true", help="새로 만들 문장 수만 센다")
    args = parser.parse_args()
    try:
        scenarios = load_scenarios(args.scenarios)
        if errors := validate_scenarios(scenarios):
            raise ScenarioAudioError("시나리오 파일 오류\n  " + "\n  ".join(errors))
        scenarios = pick_scenarios(scenarios, args.only)
        voices = voices_from(args)
        missing = missing_lines(scenarios, args.cache_dir, voices, args.tts_model)
        print(
            f"시나리오 {len(scenarios)}개, 새로 만들 문장 {len(missing)}개 "
            f"({sum(len(text) for _, text in missing)}자, ElevenLabs 호출 {len(missing)}회)"
        )
        if args.dry_run:
            return
        api_key = secret("ELEVENLABS_API_KEY", "collog-elevenlabs-api-key") if missing else ""
        asyncio.run(
            generate(
                scenarios,
                cache_dir=args.cache_dir,
                voices=voices,
                model=args.tts_model,
                api_key=api_key,
                min_speech_seconds=Settings(_env_file=None).parent_min_speech_seconds,
            )
        )
    except ScenarioAudioError as exc:
        print(f"실패: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
