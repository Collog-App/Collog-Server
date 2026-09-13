from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, or_, select, update

from app.config import Settings
from app.database import Database
from app.models import (
    AcousticAnalysisRun,
    AcousticFeature,
    AssetKind,
    AssetStatus,
    AudioAsset,
    CallRecord,
    CallState,
    ExtractionEvidence,
    HealthExtraction,
    RepeatEvent,
    Transcript,
)
from app.services.acoustics import AcousticAnalysisInput, AcousticAnalyzer
from app.services.deepgram import SttGateway, SttResult
from app.services.domain import participants_consented
from app.services.gemini import ExtractionGateway
from app.services.repeat_detector import detect_repeat_events
from app.services.signals import SignalService
from app.services.storage import StorageGateway

logger = logging.getLogger(__name__)


class ProcessingPipeline:
    async def processing_allowed(self, call_id: str) -> bool:
        async with self.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            allowed = call is not None and call.recording_enabled and (
                self.settings.mock_external_services
                or self.settings.gemini_data_processing_approved
            ) and await participants_consented(
                session, call.parent_id, call.child_id, self.settings.consent_document_version
            )
            if allowed:
                return True
            if call is not None:
                call.state = CallState.ANALYSIS_EXCLUDED.value
                call.processing_claimed_at = None
                call.processing_retry_at = None
                await session.commit()
        await self.purge_call_audio(call_id)
        return False

    def log_stt_result(self, call_id: str, speaker: str, result: SttResult) -> None:
        logger.info(
            "STT %s call=%s provider=%s speech=%.1fs segments=%d words=%d",
            speaker,
            call_id,
            result.provider,
            result.speech_seconds,
            len(result.segments),
            len(result.words),
        )
        if not self.settings.log_stt_transcript:
            return
        for segment in result.segments:
            logger.info(
                "STT %s %7.2f-%7.2fs | %s",
                speaker,
                segment.start_ms / 1000,
                segment.end_ms / 1000,
                segment.text,
            )

    def __init__(
        self,
        settings: Settings,
        database: Database,
        storage: StorageGateway,
        stt: SttGateway,
        extraction: ExtractionGateway,
        acoustics: AcousticAnalyzer,
        signals: SignalService,
    ) -> None:
        self.settings = settings
        self.database = database
        self.storage = storage
        self.stt = stt
        self.extraction = extraction
        self.acoustics = acoustics
        self.signals = signals
        self._locks: dict[str, asyncio.Lock] = {}

    async def process(self, call_id: str) -> None:
        lock = self._locks.setdefault(call_id, asyncio.Lock())
        async with lock:
            claimed_at = datetime.now(UTC)
            try:
                async with asyncio.timeout(self.settings.processing_timeout_seconds):
                    await self._process(call_id, claimed_at)
            except Exception as error:
                logger.exception("call processing failed", extra={"call_id": call_id})
                if not await self.processing_allowed(call_id):
                    return
                async with self.database.sessions() as session:
                    call = await session.get(CallRecord, call_id)
                    if (
                        call
                        and call.processing_claimed_at
                        and aware_datetime(call.processing_claimed_at) == claimed_at
                    ):
                        retry = transient_processing_error(error) and call.processing_attempts < 3
                        call.state = (
                            CallState.ENDED.value if retry else CallState.ANALYSIS_FAILED.value
                        )
                        call.processing_error = (
                            "Analysis retry pending" if retry else "Analysis failed"
                        )
                        call.processing_retry_at = (
                            datetime.now(UTC)
                            + timedelta(seconds=30 * 2 ** (call.processing_attempts - 1))
                            if retry else None
                        )
                        call.processing_claimed_at = None
                        await session.commit()
                        if not retry:
                            await self.purge_call_audio(call_id)
            finally:
                self._locks.pop(call_id, None)

    async def _process(self, call_id: str, claimed_at: datetime) -> None:
        async with self.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            if call is None or call.state in {
                CallState.ANALYZED.value,
                CallState.ANALYSIS_EXCLUDED.value,
                CallState.ANALYSIS_FAILED.value,
                CallState.PROCESSING.value,
            }:
                return
            if not call.recording_enabled or call.ended_at is None:
                return
            if (
                call.processing_retry_at
                and aware_datetime(call.processing_retry_at) > datetime.now(UTC)
            ):
                return
            if (
                not self.settings.mock_external_services
                and not self.settings.gemini_data_processing_approved
            ) or not await participants_consented(
                session, call.parent_id, call.child_id, self.settings.consent_document_version
            ):
                call.state = CallState.ANALYSIS_EXCLUDED.value
                call.recording_enabled = False
                await session.commit()
                await self.purge_call_audio(call_id)
                return
            assets = (
                await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            ).all()
            now = datetime.now(UTC)
            if call.processing_attempts >= 3 or any(
                now - aware_datetime(asset.uploaded_at or asset.created_at) >= timedelta(hours=24)
                for asset in assets
            ):
                call.state = CallState.ANALYSIS_FAILED.value
                call.processing_error = "Analysis retry limit or audio retention limit reached"
                call.processing_retry_at = None
                await session.commit()
                await self.purge_call_audio(call_id)
                return
            elapsed = (now - aware_datetime(call.ended_at)).total_seconds()
            for asset in assets:
                if asset.status != AssetStatus.PENDING.value:
                    continue
                timeout = (
                    (now - aware_datetime(asset.created_at)).total_seconds()
                    >= self.settings.upload_url_ttl_seconds
                    if asset.kind == AssetKind.DEVICE_RAW.value
                    else elapsed >= self.settings.egress_wait_seconds
                )
                if await self.storage.exists(asset.uri):
                    asset.status = AssetStatus.UPLOADED.value
                    asset.uploaded_at = now
                elif timeout:
                    asset.status = AssetStatus.FAILED.value
            await session.commit()
            egress_assets = [
                item
                for item in assets
                if item.kind
                in {
                    AssetKind.WEBRTC_EGRESS_PARENT.value,
                    AssetKind.WEBRTC_EGRESS_CHILD.value,
                }
            ]
            if any(item.status == AssetStatus.PENDING.value for item in egress_assets):
                return
            parent_egress = next(
                (
                    item
                    for item in assets
                    if item.kind == AssetKind.WEBRTC_EGRESS_PARENT.value
                    and item.status == AssetStatus.UPLOADED.value
                ),
                None,
            )
            raw_only = self.settings.allow_raw_only_analysis and parent_egress is None
            if parent_egress is None and not raw_only:
                if egress_assets or elapsed >= self.settings.egress_wait_seconds:
                    call.state = CallState.ANALYSIS_FAILED.value
                    call.processing_error = "부모 Egress 녹음이 완료되지 않았습니다"
                    await session.commit()
                    await self.purge_call_audio(call_id)
                return
            child_egress = next(
                (
                    item
                    for item in assets
                    if item.kind == AssetKind.WEBRTC_EGRESS_CHILD.value
                    and item.status == AssetStatus.UPLOADED.value
                ),
                None,
            )
            raw_asset = next(
                (
                    item
                    for item in assets
                    if item.kind == AssetKind.DEVICE_RAW.value
                    and item.status == AssetStatus.UPLOADED.value
                ),
                None,
            )
            pending_raw = any(
                item.kind == AssetKind.DEVICE_RAW.value and item.status == AssetStatus.PENDING.value
                for item in assets
            )
            if pending_raw:
                return
            if raw_asset is None:
                if raw_only:
                    if elapsed >= self.settings.upload_url_ttl_seconds:
                        call.state = CallState.ANALYSIS_FAILED.value
                        call.processing_error = "Raw audio upload expired"
                        await session.commit()
                        await self.purge_call_audio(call_id)
                    return
                if elapsed < self.settings.raw_audio_wait_seconds:
                    return
            # 여기서 PROCESSING을 조건부 UPDATE로 선점한다. `process()`의 asyncio.Lock은
            # 프로세스 안에서만 유효한데, scripts/replay_call.py는 백엔드와 별개 프로세스로
            # 같은 DB를 본다. 서버의 10초 cleanup_loop(main.py)와 replay가 같은 통화를
            # 동시에 분석하면, 먼저 끝난 쪽이 purge_call_audio로 오디오를 지워서 뒤늦은 쪽이
            # storage.read에서 NoSuchKey로 죽거나 health_extractions UNIQUE 제약에 걸린다.
            # rowcount가 0이면 다른 쪽이 이미 가져간 것이므로 조용히 물러난다.
            claimed = await session.execute(
                update(CallRecord)
                .where(
                    CallRecord.id == call_id,
                    CallRecord.state.not_in(
                        [
                            CallState.PROCESSING.value,
                            CallState.ANALYZED.value,
                            CallState.ANALYSIS_EXCLUDED.value,
                        ]
                    ),
                )
                .values(
                    state=CallState.PROCESSING.value,
                    processing_claimed_at=claimed_at,
                    processing_attempts=CallRecord.processing_attempts + 1,
                    processing_retry_at=None,
                    processing_error=None,
                )
            )
            if claimed.rowcount == 0:
                return
            await session.commit()

        # Egress가 없는 개발 환경에서는 부모 기기가 올린 분석용 PCM을 부모 음성으로 쓴다.
        # 자녀 음성이 없으므로 transcript에는 부모 발화만 남는다.
        parent_source = parent_egress or raw_asset
        parent_audio = await self.storage.read(parent_source.uri)
        if not await self.processing_allowed(call_id):
            return
        parent_stt = await self.stt.transcribe(parent_audio, parent_source.content_type, "PARENT")
        self.log_stt_result(call_id, "PARENT", parent_stt)
        stt_results = [("PARENT", parent_stt)]
        if child_egress:
            child_audio = await self.storage.read(child_egress.uri)
            if not await self.processing_allowed(call_id):
                return
            child_stt = await self.stt.transcribe(child_audio, child_egress.content_type, "CHILD")
            self.log_stt_result(call_id, "CHILD", child_stt)
            stt_results.append(("CHILD", child_stt))

        raw_segments = sorted(
            [
                {
                    "speaker": speaker,
                    "startMs": segment.start_ms,
                    "endMs": segment.end_ms,
                    "text": segment.text,
                    "words": [
                        {
                            "startMs": word.start_ms,
                            "endMs": word.end_ms,
                            "text": word.text,
                            "confidence": word.confidence,
                        }
                        for word in segment.words
                    ],
                }
                for speaker, result in stt_results
                for segment in result.segments
            ],
            key=lambda item: (item["startMs"], item["speaker"]),
        )
        segments = [
            {"segmentId": f"s{index:04d}", **item} for index, item in enumerate(raw_segments)
        ]
        repeat_events = detect_repeat_events(segments)
        parent_speech_sec = round(parent_stt.speech_seconds)
        provider = parent_stt.provider
        excluded = parent_speech_sec < self.settings.parent_min_speech_seconds

        if not await self.processing_allowed(call_id):
            return
        async with self.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            if call is None:
                return
            call.parent_speech_sec = parent_speech_sec
            transcript = await session.scalar(
                select(Transcript).where(Transcript.call_id == call_id)
            )
            if transcript is None:
                transcript = Transcript(call_id=call_id, provider=provider)
                session.add(transcript)
            transcript.provider = provider
            transcript.excluded = excluded
            transcript.exclusion_reason = "INSUFFICIENT_PARENT_SPEECH" if excluded else None
            transcript.parent_speech_sec = parent_speech_sec
            transcript.segments = segments
            await session.execute(delete(RepeatEvent).where(RepeatEvent.call_id == call_id))
            session.add_all(
                [
                    RepeatEvent(
                        call_id=call_id,
                        speaker="PARENT",
                        start_ms=event.start_ms,
                        end_ms=event.end_ms,
                        category=event.category,
                        matched_text=event.matched_text,
                        rule_id=event.rule_id,
                        confidence=event.confidence,
                        rule_version=event.rule_version,
                    )
                    for event in repeat_events
                ]
            )
            await session.commit()

        if excluded:
            async with self.database.sessions() as session:
                call = await session.get(CallRecord, call_id)
                if call:
                    call.state = CallState.ANALYSIS_EXCLUDED.value
                    call.processing_claimed_at = None
                    await session.commit()
            await self.purge_call_audio(call_id)
            return

        transcript_text = "\n".join(f"{item['speaker']}: {item['text']}" for item in segments)
        if not await self.processing_allowed(call_id):
            return
        try:
            extracted = await self.extraction.extract(segments)
            extraction_values = extracted.model_dump()
            extraction_facts = [fact.model_dump(by_alias=True) for fact in extracted.facts]
            parse_status = "OK"
            raw_transcript = None
        except Exception:
            logger.exception("health extraction failed", extra={"call_id": call_id})
            extraction_values = {
                key: None for key in ("symptom", "medication", "activity", "sleep")
            }
            extraction_facts = []
            parse_status = "FAILED"
            raw_transcript = transcript_text

        acoustic_asset = raw_asset or parent_source
        acoustic_audio = await self.storage.read(acoustic_asset.uri)
        measurements = await self.acoustics.analyze(
            AcousticAnalysisInput(
                audio=acoustic_audio,
                content_type=acoustic_asset.content_type,
                declared_sample_rate=acoustic_asset.sample_rate,
                source=(
                    "DEVICE_RAW"
                    if acoustic_asset.kind == AssetKind.DEVICE_RAW.value
                    else "WEBRTC_EGRESS"
                ),
                parent_segments=[item for item in segments if item["speaker"] == "PARENT"],
                parent_speech_seconds=parent_stt.speech_seconds,
            )
        )

        if not await self.processing_allowed(call_id):
            return
        async with self.database.sessions() as session:
            extraction = await session.scalar(
                select(HealthExtraction).where(HealthExtraction.call_id == call_id)
            )
            if extraction is None:
                extraction = HealthExtraction(call_id=call_id, parse_status=parse_status)
                session.add(extraction)
            extraction.parse_status = parse_status
            extraction.symptom = extraction_values["symptom"]
            extraction.medication = extraction_values["medication"]
            extraction.activity = extraction_values["activity"]
            extraction.sleep = extraction_values["sleep"]
            extraction.raw_transcript = raw_transcript
            evidence = await session.scalar(
                select(ExtractionEvidence).where(ExtractionEvidence.call_id == call_id)
            )
            if evidence is None:
                evidence = ExtractionEvidence(call_id=call_id)
                session.add(evidence)
            evidence.facts = extraction_facts
            evidence.schema_version = "v2"
            analysis_run = await session.scalar(
                select(AcousticAnalysisRun).where(AcousticAnalysisRun.call_id == call_id)
            )
            if analysis_run is None:
                analysis_run = AcousticAnalysisRun(
                    call_id=call_id,
                    analyzer_version=self.settings.acoustic_analyzer_version,
                    cough_detector_version="transient-heuristic-v1",
                )
                session.add(analysis_run)
            existing_metrics = set(
                await session.scalars(
                    select(AcousticFeature.metric).where(AcousticFeature.call_id == call_id)
                )
            )
            for item in measurements:
                if item.metric.value in existing_metrics:
                    continue
                session.add(
                    AcousticFeature(
                        call_id=call_id,
                        audio_source=(
                            "DEVICE_RAW"
                            if acoustic_asset.kind == AssetKind.DEVICE_RAW.value
                            else "WEBRTC_EGRESS"
                        ),
                        metric=item.metric.value,
                        value=item.value,
                        unit=item.unit,
                        status=item.status,
                        unmeasurable_reason=item.unmeasurable_reason,
                        observed_at=call.ended_at or datetime.now(UTC),
                    )
                )
            await session.flush()
            call = await session.get(CallRecord, call_id)
            if call:
                await self.signals.process_call(session, call)
                call.state = CallState.ANALYZED.value
                call.processing_claimed_at = None
            await session.commit()

        await self.purge_call_audio(call_id)

    async def purge_call_audio(self, call_id: str) -> None:
        async with self.database.sessions() as session:
            assets = (
                await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            ).all()
            for asset in assets:
                if asset.status == AssetStatus.PURGED.value:
                    if not await self.storage.exists(asset.uri):
                        continue
                try:
                    await self.storage.delete(asset.uri)
                except Exception:
                    logger.exception("audio purge failed", extra={"asset_id": asset.id})
                    continue
                asset.status = AssetStatus.PURGED.value
                asset.purged_at = datetime.now(UTC)
            call = await session.get(CallRecord, call_id)
            if call and all(asset.status == AssetStatus.PURGED.value for asset in assets):
                call.raw_audio_purged_at = datetime.now(UTC)
            await session.commit()

    async def purge_expired_audio(self) -> int:
        # 만료 기준은 통화가 끝난 시각이 아니라 오디오가 실제로 저장된 시각이다.
        # `ended_at`은 논리적인 통화 시각이라 뒤로 조작될 수 있는데(개발용 replay는 통화를
        # 몇 주 전으로 넣는다), 그걸 기준으로 삼으면 방금 올라온 오디오가 이미 만료된 것으로
        # 보여서 분석 전에 지워진다. `uploaded_at`을 쓰면 "저장된 지 24시간" 이라는 보관
        # 약속은 그대로면서 아직 분석 못 한 오디오를 먼저 지우는 일이 없다.
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        async with self.database.sessions() as session:
            disabled_call_ids = list(
                await session.scalars(
                    select(AudioAsset.call_id)
                    .join(CallRecord, CallRecord.id == AudioAsset.call_id)
                    .where(
                        CallRecord.recording_enabled.is_(False),
                        func.coalesce(AudioAsset.uploaded_at, AudioAsset.created_at) >= cutoff,
                    )
                    .distinct()
                )
            )
            call_ids = list(
                await session.scalars(
                    select(AudioAsset.call_id)
                    .where(
                        AudioAsset.status != AssetStatus.PURGED.value,
                        func.coalesce(AudioAsset.uploaded_at, AudioAsset.created_at) < cutoff,
                    )
                    .distinct()
                )
            )
        for call_id in disabled_call_ids:
            await self.purge_call_audio(call_id)
        for call_id in call_ids:
            async with self.database.sessions() as session:
                await session.execute(
                    update(CallRecord)
                    .where(
                        CallRecord.id == call_id,
                        CallRecord.state.in_([CallState.ENDED.value, CallState.PROCESSING.value]),
                    )
                    .values(
                        state=CallState.ANALYSIS_FAILED.value,
                        processing_error="Audio retention limit reached",
                        processing_claimed_at=None,
                        processing_retry_at=None,
                    )
                )
                await session.commit()
            await self.purge_call_audio(call_id)
        return len(call_ids)

    async def process_pending(self) -> int:
        await self.release_stale_claims()
        async with self.database.sessions() as session:
            call_ids = list(
                await session.scalars(
                    select(CallRecord.id)
                    .where(
                        CallRecord.state == CallState.ENDED.value,
                        CallRecord.recording_enabled.is_(True),
                    )
                    .order_by(CallRecord.ended_at)
                    .limit(100)
                )
            )
        for call_id in call_ids:
            await self.process(call_id)
        return len(call_ids)

    async def release_stale_claims(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.processing_lease_seconds)
        async with self.database.sessions() as session:
            released = await session.execute(
                update(CallRecord)
                .where(
                    CallRecord.state == CallState.PROCESSING.value,
                    or_(
                        CallRecord.processing_claimed_at.is_(None),
                        CallRecord.processing_claimed_at < cutoff,
                    ),
                )
                .values(state=CallState.ENDED.value, processing_claimed_at=None)
            )
            await session.commit()
        if released.rowcount:
            logger.info(
                "PROCESSING에 멈춰 있던 통화 %d건을 재처리 대기로 돌렸습니다", released.rowcount
            )
        return released.rowcount


def aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def transient_processing_error(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, httpx.TransportError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429} or error.response.status_code >= 500
    return error.__cause__ is not None and transient_processing_error(error.__cause__)
