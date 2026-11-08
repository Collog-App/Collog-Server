"""통화 시나리오를 실제 분석 파이프라인에 넣고 「기대 / 실제 / 통과」 표를 뽑는다. 개발 전용.

휴대폰으로 매번 통화하지 않고 evals/call_scenarios.json의 통화를 한 번에 돌린다. 매번 빈
임시 SQLite DB와 임시 저장소를 만들고, 끝나면 지운다. 운영 DB·서버는 건드리지 않는다.

가짜 모드(기본, 외부 호출 없음)
    uv run python -m scripts.run_call_scenarios
  STT와 Gemini만 바꾼다. 전사 결과는 시나리오 대사 그대로(한 대사 = 한 segment, 말 빠르기는
  공백 뺀 5자/초로 어림), Gemini 답은 대사에 적힌 `fakeFacts`(요약 = 그 대사 원문)다. 그 뒤의
  근거·의료 표현 규칙(validate_facts), 응급 안내, 부모 발화 20초 제외 판정, 리포트 조립은
  앱 코드를 그대로 탄다. 모델 판단은 시험하지 않는다.

실제 모드
    COLLOG_TEST_VOICE_PARENT=<voice id> COLLOG_TEST_VOICE_CHILD=<voice id> \\
        uv run python -m scripts.run_call_scenarios --real [--only cough]
  scenario_audio로 만든 음성(캐시가 없으면 ElevenLabs로 만든다)을 replay_call처럼
  Egress 부모·자녀 트랙과 DEVICE_RAW로 등록해 Deepgram·Gemini(가드레일 LLM 검토 포함)를
  실제로 부른다. 키는 환경변수(DEEPGRAM_API_KEY, GEMINI_API_KEY, ELEVENLABS_API_KEY) 또는 맥
  키체인(-a collog, collog-deepgram-api-key / collog-gemini-api-key /
  collog-elevenlabs-api-key)에서 읽고 출력하지 않는다. 설정은 코드 기본값과 환경변수만 쓰고
  backend/.env는 읽지 않는다. 합성 대사이므로 Gemini 데이터 처리 승인을 켜고 돈다.

결과는 터미널과 backend/.scenario-results/<시각>.md에 쓴다. 하나라도 실패하면 종료 코드 1.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from app.config import Settings
from app.consent import CONSENT_ITEMS
from app.database import Database
from app.models import (
    AssetKind,
    AssetStatus,
    AudioAsset,
    CallRecord,
    CallState,
    ConsentDecision,
    ConsentRecord,
    ExtractionEvidence,
    TimeSlot,
    Transcript,
    User,
)
from app.schemas import ExtractionFact, ExtractionPayload, GeminiExtractionResponse
from app.services.acoustics import create_acoustic_analyzer
from app.services.deepgram import SttGateway, SttResult, SttSegment, SttWord, create_stt_gateway
from app.services.gemini import (
    ExtractionGateway,
    TranscriptInput,
    create_extraction_gateway,
    normalize_transcript_input,
    validate_facts,
)
from app.services.pipeline import ProcessingPipeline
from app.services.reports import REPORT_TIMEZONE, ReportService
from app.services.signals import SignalService
from app.services.storage import create_storage
from scripts.replay_call import audio_meta
from scripts.scenario_audio import (
    BACKEND_DIR,
    CATEGORIES,
    SAMPLE_RATE,
    ScenarioAudio,
    ScenarioAudioError,
    Span,
    add_audio_arguments,
    assemble_tracks,
    generate,
    load_scenarios,
    missing_lines,
    pick_scenarios,
    secret,
    to_wav,
    validate_scenarios,
    voices_from,
)

RESULTS_DIR = BACKEND_DIR / ".scenario-results"
FAKE_CHARS_PER_SECOND = 5.0
STATE_LABELS = {
    CallState.ANALYSIS_EXCLUDED.value: "EXCLUDED",
    CallState.ANALYSIS_FAILED.value: "FAILED",
}


class ScenarioStt(SttGateway):
    """Deepgram 대신 시나리오 대사를 화자 트랙별 전사로 돌려준다. 오디오는 보지 않는다."""

    def __init__(self, spans: list[Span]) -> None:
        self.spans = spans

    async def transcribe(
        self, audio: bytes, content_type: str, speaker: Literal["PARENT", "CHILD"]
    ) -> SttResult:
        del audio, content_type
        segments = []
        for span in self.spans:
            if span.speaker != speaker:
                continue
            tokens = span.text.split()
            step = (span.end_ms - span.start_ms) / len(tokens)
            words = [
                SttWord(
                    start_ms=round(span.start_ms + index * step),
                    end_ms=round(span.start_ms + (index + 1) * step),
                    text=token,
                    confidence=1.0,
                )
                for index, token in enumerate(tokens)
            ]
            segments.append(SttSegment(span.start_ms, span.end_ms, span.text, words))
        words = [word for segment in segments for word in segment.words]
        return SttResult(
            provider="scenario-fake",
            # Deepgram과 같이 단어 구간 합으로 센다.
            speech_seconds=sum(word.end_ms - word.start_ms for word in words) / 1000,
            segments=segments,
            words=words,
        )


class ScenarioExtraction(ExtractionGateway):
    """Gemini 대신 대사의 fakeFacts로 답하고, 그 답을 앱의 규칙 검사(validate_facts)에 넣는다.

    요약은 근거 대사 원문이다. 같은 범주가 여러 대사에 있으면 근거를 합치고 마지막 대사의
    polarity를 쓴다.
    """

    def __init__(self, scenario: dict[str, Any], spans: list[Span]) -> None:
        self.lines = scenario["lines"]
        self.spans = spans

    async def extract(self, transcript: TranscriptInput) -> ExtractionPayload:
        segments = normalize_transcript_input(transcript)
        segment_ids = {
            item["startMs"]: item["id"] for item in segments if item["speaker"] == "PARENT"
        }
        claims: dict[str, dict[str, Any]] = {}
        for span in self.spans:
            for category, polarity in self.lines[span.index].get("fakeFacts", {}).items():
                claim = claims.setdefault(category, {"ids": [], "texts": []})
                claim["ids"].append(segment_ids[span.start_ms])
                claim["texts"].append(span.text)
                claim["polarity"] = polarity
        facts = [
            ExtractionFact(
                category=category,
                summary=" ".join(claim["texts"])[:240],
                polarity=claim["polarity"],
                evidence_segment_ids=claim["ids"],
            )
            for category, claim in claims.items()
        ]
        return validate_facts(GeminiExtractionResponse(facts=facts), segments)


@dataclass(slots=True)
class Check:
    name: str
    expected: str
    actual: str
    passed: bool


@dataclass(slots=True)
class Outcome:
    scenario_id: str
    description: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


@dataclass(slots=True)
class Observed:
    state: str
    parent_speech_sec: int | None
    facts: dict[str, str]
    summaries: list[str]
    safety_notice: bool
    processing_error: str | None = None


def fake_lengths(lines: list[dict[str, Any]]) -> list[int]:
    return [
        round(max(0.3, len(re.sub(r"\s", "", line["text"])) / FAKE_CHARS_PER_SECOND) * SAMPLE_RATE)
        for line in lines
    ]


def fake_tracks(scenario: dict[str, Any], out_dir: Path) -> tuple[ScenarioAudio, list[Span]]:
    """가짜 모드용 무음 트랙. 길이만 대사 구간과 맞춘다."""
    lengths = fake_lengths(scenario["lines"])
    parent, child, spans = assemble_tracks(scenario["lines"], [bytes(n * 2) for n in lengths])
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = ScenarioAudio(out_dir / "parent.wav", out_dir / "child.wav", out_dir / "raw.wav")
    for path, pcm in ((audio.parent, parent), (audio.child, child), (audio.device_raw, parent)):
        path.write_bytes(to_wav(pcm))
    return audio, spans


def scenario_settings(
    work_dir: Path, *, real: bool, deepgram_key: str, gemini_key: str
) -> Settings:
    return Settings(
        _env_file=None,
        app_env="development",
        database_url=f"sqlite+aiosqlite:///{work_dir / 'scenarios.db'}",
        storage_backend="local",
        local_storage_path=work_dir / "storage",
        mock_external_services=not real,
        gemini_data_processing_approved=real,
        deepgram_api_key=deepgram_key,
        gemini_api_key=gemini_key,
        allow_raw_only_analysis=False,
        raw_audio_wait_seconds=0,
        log_stt_transcript=False,
        question_tts_provider="ios_local",
    )


async def create_call(
    database: Database, settings: Settings, scenario: dict[str, Any], number: int, seconds: int
) -> tuple[str, str, datetime]:
    """동의를 마친 부모·자녀와 방금 끝난 통화 한 건을 만든다."""
    ended = datetime.now(UTC) - timedelta(minutes=1)
    started = ended - timedelta(seconds=seconds)
    hour = started.astimezone(REPORT_TIMEZONE).hour
    async with database.sessions() as session:
        parent = User(role="PARENT", name="부모", phone=f"0109{number:07d}")
        child = User(role="CHILD", name="자녀", phone=f"0108{number:07d}")
        session.add_all([parent, child])
        await session.flush()
        session.add_all(
            ConsentRecord(
                user_id=user.id,
                document_version=settings.consent_document_version,
                decision=ConsentDecision.GRANTED.value,
                agreed_items=list(CONSENT_ITEMS),
            )
            for user in (parent, child)
        )
        call = CallRecord(
            parent_id=parent.id,
            child_id=child.id,
            caller_id=child.id,
            state=CallState.ENDED.value,
            room_name=f"scenario-{scenario['id']}-{number}",
            recording_enabled=True,
            asked_question_ids=[],
            started_at=started,
            accepted_at=started,
            ended_at=ended,
            duration_sec=seconds,
            time_slot=(
                TimeSlot.MORNING.value if 6 <= hour <= 11 else TimeSlot.AFTERNOON_EVENING.value
            ),
        )
        session.add(call)
        await session.commit()
        return call.id, parent.id, ended


async def register_audio(
    database: Database, storage: Any, call_id: str, audio: ScenarioAudio
) -> None:
    """replay_call처럼 Egress·기기 녹음이 올라온 상태를 만든다(HTTP 업로드 대신 직접 쓴다)."""
    for kind, path in (
        (AssetKind.WEBRTC_EGRESS_PARENT.value, audio.parent),
        (AssetKind.WEBRTC_EGRESS_CHILD.value, audio.child),
        (AssetKind.DEVICE_RAW.value, audio.device_raw),
    ):
        content_type, duration, sample_rate = await asyncio.to_thread(audio_meta, path)
        key = f"calls/{call_id}/{kind.lower()}{path.suffix}"
        await storage.write(key, await asyncio.to_thread(path.read_bytes), content_type)
        async with database.sessions() as session:
            session.add(
                AudioAsset(
                    call_id=call_id,
                    kind=kind,
                    uri=storage.object_uri(key),
                    content_type=content_type,
                    duration_sec=duration,
                    sample_rate=sample_rate,
                    status=AssetStatus.UPLOADED.value,
                    uploaded_at=datetime.now(UTC),
                )
            )
            await session.commit()


async def observe(database: Database, call_id: str, parent_id: str, ended: datetime) -> Observed:
    async with database.sessions() as session:
        call = await session.get(CallRecord, call_id)
        transcript = await session.scalar(select(Transcript).where(Transcript.call_id == call_id))
        evidence = await session.scalar(
            select(ExtractionEvidence).where(ExtractionEvidence.call_id == call_id)
        )
        report = await ReportService().get_or_issue(
            session, parent_id, "WEEKLY", ended.astimezone(REPORT_TIMEZONE).date()
        )
    state = call.state if call else "MISSING"
    return Observed(
        state=STATE_LABELS.get(state, state),
        parent_speech_sec=transcript.parent_speech_sec if transcript else None,
        facts={item["category"]: item["polarity"] for item in (evidence.facts if evidence else [])},
        # 사용자에게 보이는 요약은 리포트의 conversationItems다.
        summaries=[text for items in report["conversationItems"].values() for text in items],
        safety_notice=bool(report["safetyNotices"]),
        processing_error=call.processing_error if call else None,
    )


def describe_facts(facts: dict[str, str]) -> str:
    return ", ".join(f"{key}={facts[key]}" for key in sorted(facts)) or "없음"


def evaluate(expected: dict[str, Any], observed: Observed) -> list[Check]:
    seconds = observed.parent_speech_sec
    speech = f"부모 {seconds}초" if seconds is not None else ""
    detail = ", ".join(part for part in (speech, observed.processing_error or "") if part)
    checks = [
        Check(
            "분석",
            expected["state"],
            observed.state + (f" ({detail})" if detail else ""),
            observed.state == expected["state"],
        )
    ]
    wanted = expected.get("facts", {})
    checks.append(
        Check(
            "사실",
            describe_facts(wanted),
            describe_facts(observed.facts),
            all(
                wanted.get(category) in ("ANY", observed.facts.get(category))
                for category in CATEGORIES
            ),
        )
    )
    includes = expected.get("summaryIncludes", [])
    excludes = expected.get("summaryExcludes", [])
    if includes or excludes:
        text = " / ".join(observed.summaries)
        rules = [f"포함 {pattern}" for pattern in includes] + [f"제외 {p}" for p in excludes]
        checks.append(
            Check(
                "요약",
                ", ".join(rules),
                text or "없음",
                all(re.search(pattern, text) for pattern in includes)
                and not any(re.search(pattern, text) for pattern in excludes),
            )
        )
    checks.append(
        Check(
            "응급 안내",
            "있음" if expected["safetyNotice"] else "없음",
            "있음" if observed.safety_notice else "없음",
            observed.safety_notice == expected["safetyNotice"],
        )
    )
    return checks


async def run_scenarios(
    scenarios: list[dict[str, Any]],
    *,
    real: bool = False,
    audio: dict[str, ScenarioAudio] | None = None,
    deepgram_key: str = "",
    gemini_key: str = "",
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    with tempfile.TemporaryDirectory(prefix="collog-scenarios-") as tmp:
        work_dir = Path(tmp)
        settings = scenario_settings(
            work_dir, real=real, deepgram_key=deepgram_key, gemini_key=gemini_key
        )
        database = Database(settings.database_url)
        try:
            await database.ensure_schema(auto_reset=True)
            storage = create_storage(settings)
            acoustics = create_acoustic_analyzer(settings)
            signals = SignalService(settings)
            stt = create_stt_gateway(settings) if real else None
            extraction = create_extraction_gateway(settings) if real else None
            for number, scenario in enumerate(scenarios, start=1):
                outcome = Outcome(scenario["id"], scenario["description"])
                outcomes.append(outcome)
                try:
                    if real:
                        assert audio is not None and stt is not None and extraction is not None
                        tracks = audio[scenario["id"]]
                        scenario_stt, scenario_extraction = stt, extraction
                    else:
                        tracks, spans = await asyncio.to_thread(
                            fake_tracks, scenario, work_dir / "tracks" / scenario["id"]
                        )
                        scenario_stt = ScenarioStt(spans)
                        scenario_extraction = ScenarioExtraction(scenario, spans)
                    _, seconds, _ = await asyncio.to_thread(audio_meta, tracks.parent)
                    call_id, parent_id, ended = await create_call(
                        database, settings, scenario, number, round(seconds or 0)
                    )
                    await register_audio(database, storage, call_id, tracks)
                    pipeline = ProcessingPipeline(
                        settings,
                        database,
                        storage,
                        scenario_stt,
                        scenario_extraction,
                        acoustics,
                        signals,
                    )
                    await pipeline.process(call_id)
                    observed = await observe(database, call_id, parent_id, ended)
                    outcome.checks = evaluate(scenario["expected"], observed)
                except Exception as exc:  # 한 시나리오가 죽어도 나머지는 돈다.
                    outcome.checks = [
                        Check("실행", "오류 없음", f"{type(exc).__name__}: {exc}"[:200], False)
                    ]
        finally:
            await database.close()
    return outcomes


def cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render(outcomes: list[Outcome], mode: str, started: datetime) -> str:
    passed = sum(outcome.passed for outcome in outcomes)
    lines = [
        f"# 통화 시나리오 결과 ({mode})",
        "",
        f"- 실행: {started.astimezone(REPORT_TIMEZONE):%Y-%m-%d %H:%M:%S} KST",
        f"- 통과: {passed} / {len(outcomes)}",
        "",
        "| 시나리오 | 항목 | 기대 | 실제 | 판정 |",
        "|---|---|---|---|---|",
    ]
    for outcome in outcomes:
        for index, check in enumerate(outcome.checks):
            lines.append(
                "| "
                + " | ".join(
                    (
                        outcome.scenario_id if index == 0 else "",
                        check.name,
                        cell(check.expected),
                        cell(check.actual),
                        "통과" if check.passed else "실패",
                    )
                )
                + " |"
            )
    return "\n".join(lines) + "\n"


async def run_cli(args: argparse.Namespace) -> list[Outcome]:
    scenarios = load_scenarios(args.scenarios)
    if errors := validate_scenarios(scenarios):
        raise ScenarioAudioError("시나리오 파일 오류\n  " + "\n  ".join(errors))
    scenarios = pick_scenarios(scenarios, args.only)
    if not args.real:
        return await run_scenarios(scenarios)

    keys = {
        "DEEPGRAM_API_KEY": await asyncio.to_thread(
            secret, "DEEPGRAM_API_KEY", "collog-deepgram-api-key"
        ),
        "GEMINI_API_KEY": await asyncio.to_thread(
            secret, "GEMINI_API_KEY", "collog-gemini-api-key"
        ),
    }
    voices = voices_from(args)
    missing = await asyncio.to_thread(
        missing_lines, scenarios, args.cache_dir, voices, args.tts_model
    )
    if missing:
        keys["ELEVENLABS_API_KEY"] = await asyncio.to_thread(
            secret, "ELEVENLABS_API_KEY", "collog-elevenlabs-api-key"
        )
    if absent := [name for name, value in keys.items() if not value]:
        raise ScenarioAudioError(f"실제 모드에 필요한 키가 없습니다: {', '.join(absent)}")
    print(f"새로 만들 문장 {len(missing)}개, 시나리오 {len(scenarios)}개")
    audio = await generate(
        scenarios,
        cache_dir=args.cache_dir,
        voices=voices,
        model=args.tts_model,
        api_key=keys.get("ELEVENLABS_API_KEY", ""),
        min_speech_seconds=Settings(_env_file=None).parent_min_speech_seconds,
    )
    return await run_scenarios(
        scenarios,
        real=True,
        audio=audio,
        deepgram_key=keys["DEEPGRAM_API_KEY"],
        gemini_key=keys["GEMINI_API_KEY"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="통화 시나리오를 분석 파이프라인에 넣고 기대 대 실제 표를 만든다"
    )
    add_audio_arguments(parser)
    parser.add_argument(
        "--real", action="store_true", help="생성 음성으로 Deepgram·Gemini를 실제로 부른다"
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()
    started = datetime.now(UTC)
    try:
        outcomes = asyncio.run(run_cli(args))
    except ScenarioAudioError as exc:
        print(f"실패: {exc}")
        raise SystemExit(2) from exc
    table = render(outcomes, "실제 모드" if args.real else "가짜 모드", started)
    print(table)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    path = args.results_dir / f"{started.astimezone(REPORT_TIMEZONE):%Y%m%d-%H%M%S}.md"
    path.write_text(table, encoding="utf-8")
    print(f"저장: {path}")
    raise SystemExit(0 if all(outcome.passed for outcome in outcomes) else 1)


if __name__ == "__main__":
    main()
