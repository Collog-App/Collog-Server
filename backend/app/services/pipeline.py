from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import Database
from app.models import (
    AcousticAnalysisRun,
    AcousticFeature,
    AnalysisMode,
    AssetKind,
    AssetStatus,
    AudioAsset,
    CallParticipant,
    CallRecord,
    CallState,
    ExtractionEvidence,
    HealthExtraction,
    ParticipantStatus,
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
                if await close_participants(
                    session, call_id, ParticipantStatus.EXCLUDED, "ANALYSIS_NOT_ALLOWED"
                ):
                    # 먼저 끝난 참여자가 있으면 그 사람의 분석으로 끝난 통화다.
                    call.state = CallState.ANALYZED.value
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
                        if not retry and await close_participants(
                            session, call_id, ParticipantStatus.FAILED, "ANALYSIS_ERROR"
                        ):
                            # 먼저 끝난 참여자가 있으면 그 사람의 분석으로 끝난 통화다.
                            call.state = CallState.ANALYZED.value
                            call.processing_error = None
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
                if await close_participants(
                    session, call_id, ParticipantStatus.EXCLUDED, "ANALYSIS_NOT_ALLOWED"
                ):
                    call.state = CallState.ANALYZED.value
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
                if await close_participants(
                    session, call_id, ParticipantStatus.FAILED, "RETRY_OR_RETENTION_LIMIT"
                ):
                    call.state = CallState.ANALYZED.value
                    call.processing_error = None
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
            if call.analysis_mode == AnalysisMode.BOTH.value:
                await self._process_both(session, call, assets, elapsed, claimed_at)
                return
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
            if not await self._claim(session, call_id, claimed_at):
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

        segments = merge_segments(stt_results)
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
                        subject_user_id=call.parent_id,
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
                extraction = HealthExtraction(
                    call_id=call_id, subject_user_id=call.parent_id, parse_status=parse_status
                )
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
                evidence = ExtractionEvidence(call_id=call_id, subject_user_id=call.parent_id)
                session.add(evidence)
            evidence.facts = extraction_facts
            evidence.schema_version = "v2"
            analysis_run = await session.scalar(
                select(AcousticAnalysisRun).where(AcousticAnalysisRun.call_id == call_id)
            )
            if analysis_run is None:
                analysis_run = AcousticAnalysisRun(
                    call_id=call_id,
                    subject_user_id=call.parent_id,
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
                        subject_user_id=call.parent_id,
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

    async def _claim(self, session: AsyncSession, call_id: str, claimed_at: datetime) -> bool:
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
        return claimed.rowcount != 0

    async def _process_both(
        self,
        session: AsyncSession,
        call: CallRecord,
        assets: list[AudioAsset],
        elapsed: float,
        claimed_at: datetime,
    ) -> None:
        """두 참여자를 각자의 분석 대상으로 처리한다(S3).

        참여자마다 자기 Egress 트랙으로 전사·추출하고, 음향은 자기 원시 오디오(없으면 Egress)로
        잰다. 전사와 참여자별 추출은 끝나는 대로 커밋해 재시도 때 외부 서비스를 다시 부르지 않는다.
        원본 오디오는 모든 참여자가 끝난 뒤에만 지운다.
        """
        call_id = call.id

        def uploaded(user_id: str, raw: bool) -> AudioAsset | None:
            return next(
                (
                    item
                    for item in assets
                    if item.owner_user_id == user_id
                    and (item.kind == AssetKind.DEVICE_RAW.value) == raw
                    and item.status == AssetStatus.UPLOADED.value
                ),
                None,
            )

        participants = list(
            await session.scalars(
                select(CallParticipant)
                .where(CallParticipant.call_id == call_id)
                .order_by(CallParticipant.speaker_label.desc())
            )
        )
        pending = [
            item for item in participants if item.analysis_status == ParticipantStatus.PENDING.value
        ]
        egress_assets = [item for item in assets if item.kind != AssetKind.DEVICE_RAW.value]
        if any(item.status == AssetStatus.PENDING.value for item in egress_assets):
            return
        sources = {
            item.user_id: source
            for item in pending
            if (source := uploaded(item.user_id, raw=False)) is not None
        }
        # 자기 Egress 가 없는 참여자는 EGRESS_MISSING 으로 끝난다(raw-only 개발 모드는 SINGLE 만).
        missing = [item for item in pending if item.user_id not in sources]
        if missing and not egress_assets and elapsed < self.settings.egress_wait_seconds:
            return
        if any(
            item.kind == AssetKind.DEVICE_RAW.value and item.status == AssetStatus.PENDING.value
            for item in assets
        ):
            return
        raws = {user_id: uploaded(user_id, raw=True) for user_id in sources}
        if None in raws.values() and elapsed < self.settings.raw_audio_wait_seconds:
            return
        if not await self._claim(session, call_id, claimed_at):
            return
        for item in missing:
            item.analysis_status = ParticipantStatus.FAILED.value
            item.exclusion_reason = "EGRESS_MISSING"
            item.updated_at = datetime.now(UTC)
        await session.commit()

        analyzed = [item for item in pending if item.user_id in sources]
        if analyzed:
            async with self.database.sessions() as session:
                transcript = await session.scalar(
                    select(Transcript).where(Transcript.call_id == call_id)
                )
            segments = (
                transcript.segments
                if transcript is not None
                else await self._transcribe_both(call_id, analyzed, sources)
            )
            if segments is None:
                return
            transient: Exception | None = None
            for item in analyzed:
                try:
                    allowed = await self._analyze_participant(
                        call_id, item.id, segments, sources[item.user_id], raws[item.user_id]
                    )
                except Exception as error:
                    logger.exception(
                        "participant analysis failed",
                        extra={"call_id": call_id, "user_id": item.user_id},
                    )
                    if transient_processing_error(error):
                        # 다른 참여자는 마저 끝내고 재시도로 넘긴다.
                        transient = transient or error
                        continue
                    async with self.database.sessions() as session:
                        row = await session.get(CallParticipant, item.id)
                        row.analysis_status = ParticipantStatus.FAILED.value
                        row.exclusion_reason = "ANALYSIS_ERROR"
                        row.updated_at = datetime.now(UTC)
                        await session.commit()
                    continue
                if not allowed:
                    return
            if transient is not None:
                raise transient

        async with self.database.sessions() as session:
            statuses = set(
                await session.scalars(
                    select(CallParticipant.analysis_status).where(
                        CallParticipant.call_id == call_id
                    )
                )
            )
            call = await session.get(CallRecord, call_id)
            if call is None:
                return
            if ParticipantStatus.ANALYZED.value in statuses:
                call.state = CallState.ANALYZED.value
            elif ParticipantStatus.FAILED.value in statuses:
                call.state = CallState.ANALYSIS_FAILED.value
                call.processing_error = "Analysis failed"
            else:
                call.state = CallState.ANALYSIS_EXCLUDED.value
            call.processing_claimed_at = None
            await session.commit()
        await self.purge_call_audio(call_id)

    async def _transcribe_both(
        self,
        call_id: str,
        participants: list[CallParticipant],
        sources: dict[str, AudioAsset],
    ) -> list[dict[str, Any]] | None:
        results = []
        for item in participants:
            source = sources[item.user_id]
            audio = await self.storage.read(source.uri)
            if not await self.processing_allowed(call_id):
                return None
            result = await self.stt.transcribe(audio, source.content_type, item.speaker_label)
            self.log_stt_result(call_id, item.speaker_label, result)
            results.append((item, result))
        segments = merge_segments([(item.speaker_label, result) for item, result in results])
        speech = {item.id: round(result.speech_seconds) for item, result in results}
        parent_speech_sec = next(
            (speech[item.id] for item, _ in results if item.speaker_label == "PARENT"), 0
        )
        excluded = all(value < self.settings.parent_min_speech_seconds for value in speech.values())
        if not await self.processing_allowed(call_id):
            return None
        async with self.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            if call is None:
                return None
            call.parent_speech_sec = parent_speech_sec
            session.add(
                Transcript(
                    call_id=call_id,
                    provider=results[0][1].provider,
                    excluded=excluded,
                    exclusion_reason="INSUFFICIENT_SPEECH" if excluded else None,
                    parent_speech_sec=parent_speech_sec,
                    segments=segments,
                )
            )
            for item in participants:
                row = await session.get(CallParticipant, item.id)
                row.speech_sec = speech[item.id]
                row.updated_at = datetime.now(UTC)
            await session.commit()
        return segments

    async def _analyze_participant(
        self,
        call_id: str,
        participant_id: str,
        segments: list[dict[str, Any]],
        source: AudioAsset,
        raw_asset: AudioAsset | None,
    ) -> bool:
        """한 참여자를 분석한다. 도중에 분석이 막히면(동의 철회 등) False."""
        async with self.database.sessions() as session:
            participant = await session.get(CallParticipant, participant_id)
            subject_id = participant.user_id
            speech_sec = participant.speech_sec or 0
            if speech_sec < self.settings.parent_min_speech_seconds:
                participant.analysis_status = ParticipantStatus.EXCLUDED.value
                participant.exclusion_reason = "INSUFFICIENT_SPEECH"
                participant.updated_at = datetime.now(UTC)
                await session.commit()
                return True
            extracted_before = await session.scalar(
                select(HealthExtraction.id).where(
                    HealthExtraction.call_id == call_id,
                    HealthExtraction.subject_user_id == subject_id,
                )
            )
        mine = segments_for_subject(segments, participant.speaker_label)
        if extracted_before is None:
            if not await self.processing_allowed(call_id):
                return False
            try:
                extracted = await self.extraction.extract(mine)
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
                raw_transcript = "\n".join(
                    f"{item['speaker']}: {item['text']}" for item in segments
                )
            async with self.database.sessions() as session:
                session.add_all(
                    [
                        HealthExtraction(
                            call_id=call_id,
                            subject_user_id=subject_id,
                            parse_status=parse_status,
                            symptom=extraction_values["symptom"],
                            medication=extraction_values["medication"],
                            activity=extraction_values["activity"],
                            sleep=extraction_values["sleep"],
                            raw_transcript=raw_transcript,
                        ),
                        ExtractionEvidence(
                            call_id=call_id,
                            subject_user_id=subject_id,
                            facts=extraction_facts,
                            schema_version="v2",
                        ),
                    ]
                )
                # 재시도 때 Gemini 를 다시 부르지 않도록 여기서 남긴다.
                await session.commit()

        acoustic_asset = raw_asset or source
        audio_source = (
            "DEVICE_RAW" if acoustic_asset.kind == AssetKind.DEVICE_RAW.value else "WEBRTC_EGRESS"
        )
        measurements = await self.acoustics.analyze(
            AcousticAnalysisInput(
                audio=await self.storage.read(acoustic_asset.uri),
                content_type=acoustic_asset.content_type,
                declared_sample_rate=acoustic_asset.sample_rate,
                source=audio_source,
                parent_segments=[item for item in mine if item["speaker"] == "PARENT"],
                parent_speech_seconds=float(speech_sec),
            )
        )
        repeat_events = detect_repeat_events(mine)
        if not await self.processing_allowed(call_id):
            return False
        async with self.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            session.add_all(
                [
                    RepeatEvent(
                        call_id=call_id,
                        subject_user_id=subject_id,
                        speaker=participant.speaker_label,
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
            session.add(
                AcousticAnalysisRun(
                    call_id=call_id,
                    subject_user_id=subject_id,
                    analyzer_version=self.settings.acoustic_analyzer_version,
                    cough_detector_version="transient-heuristic-v1",
                )
            )
            session.add_all(
                [
                    AcousticFeature(
                        call_id=call_id,
                        subject_user_id=subject_id,
                        audio_source=audio_source,
                        metric=item.metric.value,
                        value=item.value,
                        unit=item.unit,
                        status=item.status,
                        unmeasurable_reason=item.unmeasurable_reason,
                        observed_at=call.ended_at or datetime.now(UTC),
                    )
                    for item in measurements
                ]
            )
            await session.flush()
            await self.signals.process_call(session, call, subject_id)
            row = await session.get(CallParticipant, participant_id)
            row.analysis_status = ParticipantStatus.ANALYZED.value
            row.updated_at = datetime.now(UTC)
            # 음향·되묻기·기준선과 참여자 상태를 한 번에 남긴다.
            await session.commit()
        return True

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
                if await close_participants(
                    session, call_id, ParticipantStatus.FAILED, "AUDIO_RETENTION_LIMIT"
                ):
                    await session.execute(
                        update(CallRecord)
                        .where(
                            CallRecord.id == call_id,
                            CallRecord.state == CallState.ANALYSIS_FAILED.value,
                        )
                        .values(state=CallState.ANALYZED.value, processing_error=None)
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


def merge_segments(stt_results: list[tuple[str, SttResult]]) -> list[dict[str, Any]]:
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
    return [{"segmentId": f"s{index:04d}", **item} for index, item in enumerate(raw_segments)]


def segments_for_subject(
    segments: list[dict[str, Any]], speaker_label: str
) -> list[dict[str, Any]]:
    """분석할 사람의 발화를 PARENT, 상대 발화를 CHILD 로 바꾼다.

    추출 프롬프트·검증기·mock 이 PARENT 를 분석 대상으로 읽으므로 그대로 쓴다. segmentId 는
    그대로 두어 근거가 저장된 전사를 가리킨다.
    """
    return [
        {**item, "speaker": "PARENT" if item["speaker"] == speaker_label else "CHILD"}
        for item in segments
    ]


async def close_participants(
    session: AsyncSession, call_id: str, status: ParticipantStatus, reason: str
) -> bool:
    """아직 끝나지 않은 참여자를 끝내고, 이미 분석을 마친 참여자가 있는지 돌려준다.

    SINGLE 통화에는 참여자 행이 없어 아무 일도 없고 False 다.
    """
    await session.execute(
        update(CallParticipant)
        .where(
            CallParticipant.call_id == call_id,
            CallParticipant.analysis_status == ParticipantStatus.PENDING.value,
        )
        .values(analysis_status=status.value, exclusion_reason=reason, updated_at=datetime.now(UTC))
    )
    analyzed = await session.scalar(
        select(CallParticipant.id)
        .where(
            CallParticipant.call_id == call_id,
            CallParticipant.analysis_status == ParticipantStatus.ANALYZED.value,
        )
        .limit(1)
    )
    return analyzed is not None


def aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def transient_processing_error(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, httpx.TransportError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429} or error.response.status_code >= 500
    return error.__cause__ is not None and transient_processing_error(error.__cause__)
