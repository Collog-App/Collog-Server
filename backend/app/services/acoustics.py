from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import wave
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from app.config import Settings
from app.models import Metric


@dataclass(slots=True)
class AcousticMeasurement:
    metric: Metric
    value: float | None
    unit: str
    status: str
    unmeasurable_reason: str | None


@dataclass(slots=True)
class AcousticAnalysisInput:
    audio: bytes
    content_type: str
    declared_sample_rate: int | None
    source: Literal["DEVICE_RAW", "WEBRTC_EGRESS"]
    parent_segments: list[dict[str, Any]]
    parent_speech_seconds: float
    # 자녀 트랙 STT 결과. None은 자녀 트랙이 없다는 뜻이고, []는 자녀가 말하지 않았다는 뜻이다.
    child_segments: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class Waveform:
    samples: np.ndarray
    sample_rate: int


class AudioDecodeError(ValueError):
    pass


class AcousticAnalyzer:
    async def analyze(self, item: AcousticAnalysisInput) -> list[AcousticMeasurement]:
        raise NotImplementedError


class ModelUnavailable(RuntimeError):
    pass


def cough_unit(settings: Settings) -> str:
    """detector마다 세는 대상이 다르므로 단위도 함께 바뀐다.

    `transient-heuristic-v1`은 transient를 세어 `회`, HeAR detector는 2초 window가 겹쳐
    개별 기침을 분리하지 못하므로 연속 검출 `구간`을 센다. 기준선은 같은 analyzer version
    끼리만 비교하므로 단위가 섞이지 않는다.
    """
    return "구간" if settings.cough_detector == "hear-event-detector-small-v1" else "회"


class HearCoughDetector:
    """HeAR health acoustic event detector(`google/hear`의 `event_detector_small`)의 ONNX 변환본.

    2초/16 kHz mono clip마다 8 class 확률을 낸다. 0번이 `Cough`이고 `Throat Clear`, `Laugh`,
    `Sneeze`가 별도 class로 나오므로 hard negative를 무엇으로 오인했는지 구분할 수 있다.

    window가 2초라 "기침이 있는가"에는 답하지만 "몇 회인가"에는 답하지 못한다. 기침 한 번이
    앞뒤 window를 모두 양성으로 만들기 때문이다. 그래서 여기서 세는 값은 기침 횟수가 아니라
    연속 검출 구간의 수다.
    """

    LABELS = (
        "Cough",
        "Snore",
        "Baby Cough",
        "Breathe",
        "Sneeze",
        "Throat Clear",
        "Laugh",
        "Speech",
    )
    COUGH_INDEX = 0
    CLIP_SAMPLES = 32_000

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: Any | None = None
        self._input_name: str = ""

    def _load(self) -> Any:
        if self._session is not None:
            return self._session
        path = self.settings.cough_model_path
        if not path.is_file():
            raise ModelUnavailable("MODEL_UNAVAILABLE")
        if self.settings.cough_model_sha256:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != self.settings.cough_model_sha256:
                raise ModelUnavailable("MODEL_CHECKSUM_MISMATCH")
        import onnxruntime

        session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self._input_name = session.get_inputs()[0].name
        self._session = session
        return session

    def scores(self, samples: np.ndarray) -> np.ndarray:
        """clip별 8 class 확률을 `(clips, 8)`로 돌려준다."""
        session = self._load()
        clips = frame_clips(samples, self.CLIP_SAMPLES, self._hop_samples())
        # 내보낸 signature가 batch 1로 고정되어 있어 clip 단위로 넣는다.
        return np.stack(
            [session.run(None, {self._input_name: clip[None, :]})[0][0] for clip in clips]
        )

    def _hop_samples(self) -> int:
        return max(1, round(self.settings.cough_window_hop_seconds * 16_000))

    def detect(self, samples: np.ndarray) -> AcousticMeasurement:
        try:
            scores = self.scores(samples)
        except ModelUnavailable as exc:
            return unmeasurable(Metric.COUGH_EVENTS, cough_unit(self.settings), str(exc))
        above = scores[:, self.COUGH_INDEX] >= self.settings.cough_detection_threshold
        count = merge_patch_events(
            np.flatnonzero(above),
            self._hop_samples() / 16_000,
            self.settings.cough_merge_gap_seconds,
        )
        return measured(Metric.COUGH_EVENTS, float(count), cough_unit(self.settings))


def frame_clips(samples: np.ndarray, clip_samples: int, hop_samples: int) -> np.ndarray:
    if samples.size < clip_samples:
        samples = np.pad(samples, (0, clip_samples - samples.size))
    starts = range(0, samples.size - clip_samples + 1, hop_samples)
    return np.stack(
        [
            np.ascontiguousarray(samples[start : start + clip_samples], dtype=np.float32)
            for start in starts
        ]
    )


class CollogAcousticAnalyzer(AcousticAnalyzer):
    """Versioned, conservative prototype analyzer for the four hackathon metrics.

    Speech timing metrics come from Deepgram. F0 and cough-candidate metrics only use the
    consented iOS PCM WAV. Every failure is explicit; this class never fabricates a fallback value.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cough_detector = (
            HearCoughDetector(settings)
            if settings.cough_detector == "hear-event-detector-small-v1"
            else None
        )

    async def analyze(self, item: AcousticAnalysisInput) -> list[AcousticMeasurement]:
        measurements = await self._analyze(item)
        if self.settings.cough_detector_validated:
            return measurements
        # 검증 전까지 어떤 경로로 계산됐든 기침 값은 내보내지 않는다. 품질 실패 사유보다
        # detector 미검증이 우선하는 사실이므로 이유도 함께 덮어쓴다.
        return [
            unmeasurable(Metric.COUGH_EVENTS, cough_unit(self.settings), "DETECTOR_NOT_VALIDATED")
            if measurement.metric is Metric.COUGH_EVENTS
            else measurement
            for measurement in measurements
        ]

    async def _analyze(self, item: AcousticAnalysisInput) -> list[AcousticMeasurement]:
        if item.parent_speech_seconds < self.settings.parent_min_speech_seconds:
            return [
                unmeasurable(metric, unit, "INSUFFICIENT_PARENT_SPEECH")
                for metric, unit in (
                    (Metric.SPEECH_RATE, "음절/분"),
                    (Metric.PAUSE_RATIO, "%"),
                    (Metric.F0_VARIATION, "semitone_mad"),
                    (Metric.COUGH_EVENTS, cough_unit(self.settings)),
                )
            ]
        timing = [self._speech_rate(item), self._pause_ratio(item)]
        if item.source != "DEVICE_RAW":
            return timing + [
                unmeasurable(Metric.F0_VARIATION, "semitone_mad", "SOURCE_NOT_RAW"),
                unmeasurable(Metric.COUGH_EVENTS, cough_unit(self.settings), "SOURCE_NOT_RAW"),
            ]
        if item.content_type.split(";", 1)[0].strip().lower() not in {
            "audio/wav",
            "audio/wave",
            "audio/x-wav",
        }:
            return timing + [
                unmeasurable(Metric.F0_VARIATION, "semitone_mad", "UNSUPPORTED_AUDIO_FORMAT"),
                unmeasurable(
                    Metric.COUGH_EVENTS, cough_unit(self.settings), "UNSUPPORTED_AUDIO_FORMAT"
                ),
            ]
        try:
            waveform = await asyncio.to_thread(
                decode_pcm_wav, item.audio, item.declared_sample_rate
            )
        except AudioDecodeError as exc:
            reason = str(exc) or "INVALID_AUDIO"
            return timing + [
                unmeasurable(Metric.F0_VARIATION, "semitone_mad", reason),
                unmeasurable(Metric.COUGH_EVENTS, cough_unit(self.settings), reason),
            ]

        quality_reason = waveform_quality_reason(waveform, self.settings)
        if quality_reason:
            return timing + [
                unmeasurable(Metric.F0_VARIATION, "semitone_mad", quality_reason),
                unmeasurable(Metric.COUGH_EVENTS, cough_unit(self.settings), quality_reason),
            ]
        f0, cough = await asyncio.to_thread(self._waveform_metrics, waveform)
        return timing + [f0, cough]

    def _speech_rate(self, item: AcousticAnalysisInput) -> AcousticMeasurement:
        words = valid_words(item.parent_segments)
        syllables = sum(len(re.findall(r"[가-힣]", str(word.get("text", "")))) for word in words)
        articulation_seconds = (
            sum(max(0, int(word.get("endMs", 0)) - int(word.get("startMs", 0))) for word in words)
            / 1000
        )
        if syllables < 10 or articulation_seconds < 5:
            return unmeasurable(Metric.SPEECH_RATE, "음절/분", "INSUFFICIENT_WORD_TIMING")
        return measured(Metric.SPEECH_RATE, 60 * syllables / articulation_seconds, "음절/분")

    def _pause_ratio(self, item: AcousticAnalysisInput) -> AcousticMeasurement:
        """부모가 자녀 발화 없이 이어서 말한 구간 안의 휴지 비율.

        Deepgram은 쉬는 지점에서 segment를 나누므로 segment 안의 단어 간격만 세면 휴지가 대부분
        경계로 빠져 값이 0이 된다. 그래서 자녀 발화가 끼지 않고 `pause_max_gap_ms` 이하로 이어진
        부모 segment들을 한 묶음으로 보고 묶음 안의 모든 단어 간격을 센다. 자녀가 끼어든 간격은
        턴 전환이라 세지 않는다. 자녀 트랙이 없으면 이를 구분할 수 없으므로 측정하지 않는다.
        """
        if item.child_segments is None:
            return unmeasurable(Metric.PAUSE_RATIO, "%", "NO_CHILD_TRACK")
        child_spans = [
            (int(segment.get("startMs", 0)), int(segment.get("endMs", 0)))
            for segment in item.child_segments
        ]
        runs: list[list[dict[str, Any]]] = []
        for segment in sorted(item.parent_segments, key=lambda seg: int(seg.get("startMs", 0))):
            words = sorted(valid_words([segment]), key=lambda word: int(word.get("startMs", 0)))
            if not words:
                continue
            if runs:
                gap_start = int(runs[-1][-1].get("endMs", 0))
                gap_end = int(words[0].get("startMs", 0))
                child_spoke = any(start < gap_end and end > gap_start for start, end in child_spans)
                if not child_spoke and gap_end - gap_start <= self.settings.pause_max_gap_ms:
                    runs[-1].extend(words)
                    continue
            runs.append(words)

        pause_ms = 0
        window_ms = 0
        for words in runs:
            if len(words) < 2:
                continue
            current_window = int(words[-1].get("endMs", 0)) - int(words[0].get("startMs", 0))
            if current_window <= 0:
                continue
            window_ms += current_window
            for previous, current in zip(words, words[1:], strict=False):
                gap = int(current.get("startMs", 0)) - int(previous.get("endMs", 0))
                if self.settings.pause_min_gap_ms <= gap <= self.settings.pause_max_gap_ms:
                    pause_ms += gap
        if window_ms <= 0:
            return unmeasurable(Metric.PAUSE_RATIO, "%", "INSUFFICIENT_WORD_TIMING")
        return measured(Metric.PAUSE_RATIO, pause_ms / window_ms * 100, "%")

    def _waveform_metrics(
        self, waveform: Waveform
    ) -> tuple[AcousticMeasurement, AcousticMeasurement]:
        samples = resample_16k(waveform.samples, waveform.sample_rate)
        cough = (
            self.cough_detector.detect(samples)
            if self.cough_detector is not None
            else self._cough_candidates(samples, 16_000)
        )
        return self._f0_variation(samples, 16_000), cough

    def _f0_variation(self, samples: np.ndarray, sample_rate: int) -> AcousticMeasurement:
        import librosa

        f0, voiced, probability = librosa.pyin(
            samples,
            fmin=65,
            fmax=400,
            sr=sample_rate,
            frame_length=2048,
            hop_length=256,
            fill_na=np.nan,
        )
        mask = voiced & np.isfinite(f0) & (probability >= 0.8)
        valid = np.asarray(f0[mask], dtype=np.float64)
        if valid.size * 256 / sample_rate < 2:
            return unmeasurable(Metric.F0_VARIATION, "semitone_mad", "INSUFFICIENT_VOICED_AUDIO")
        jumps = np.abs(12 * np.log2(valid[1:] / valid[:-1])) if valid.size > 1 else np.array([])
        if jumps.size and float(np.mean(jumps > 12)) > 0.15:
            return unmeasurable(Metric.F0_VARIATION, "semitone_mad", "PITCH_UNSTABLE")
        center_hz = float(np.median(valid))
        semitones = 12 * np.log2(valid / center_hz)
        center = float(np.median(semitones))
        spread = 1.4826 * float(np.median(np.abs(semitones - center)))
        return measured(Metric.F0_VARIATION, spread, "semitone_mad")

    def _cough_candidates(self, samples: np.ndarray, sample_rate: int) -> AcousticMeasurement:
        """transient-heuristic-v1. 검증 실패로 `analyze`가 결과를 덮어쓰지만, calibration
        harness가 회귀를 측정할 수 있도록 계산 경로는 그대로 둔다."""
        frame_length = round(0.96 * sample_rate)
        hop_length = round(0.48 * sample_rate)
        if samples.size < frame_length:
            return unmeasurable(Metric.COUGH_EVENTS, "회", "AUDIO_TOO_SHORT")
        frames = np.lib.stride_tricks.sliding_window_view(samples, frame_length)[::hop_length]
        if not len(frames):
            return unmeasurable(Metric.COUGH_EVENTS, "회", "AUDIO_TOO_SHORT")
        window = np.hanning(frame_length)
        rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
        db = 20 * np.log10(rms + 1e-12)
        median_db = float(np.median(db))
        energy_score = np.clip((db - median_db - 6) / 18, 0, 1)
        spectrum = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
        frequencies = np.fft.rfftfreq(frame_length, 1 / sample_rate)
        total_power = np.sum(spectrum, axis=1) + 1e-12
        high_ratio = np.sum(spectrum[:, frequencies >= 1_000], axis=1) / total_power
        high_score = np.clip((high_ratio - 0.15) / 0.55, 0, 1)
        zcr = np.mean(np.diff(np.signbit(frames), axis=1), axis=1)
        zcr_score = np.clip((zcr - 0.03) / 0.22, 0, 1)
        crest = np.max(np.abs(frames), axis=1) / (rms + 1e-9)
        crest_score = np.clip((crest - 3) / 8, 0, 1)
        scores = 0.4 * energy_score + 0.25 * high_score + 0.2 * zcr_score + 0.15 * crest_score
        candidates = np.flatnonzero(scores >= self.settings.cough_score_threshold)
        count = merge_patch_events(candidates, hop_length / sample_rate, merge_gap_seconds=0.75)
        return measured(Metric.COUGH_EVENTS, float(count), "회")


def valid_words(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        word
        for segment in segments
        for word in segment.get("words", [])
        if str(word.get("text", "")).strip()
        and (word.get("confidence") is None or float(word["confidence"]) >= 0.5)
        and int(word.get("endMs", 0)) > int(word.get("startMs", 0))
    ]


def decode_pcm_wav(audio: bytes, declared_sample_rate: int | None = None) -> Waveform:
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise AudioDecodeError("INVALID_AUDIO")
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (EOFError, wave.Error) as exc:
        raise AudioDecodeError("INVALID_AUDIO") from exc
    if channels not in {1, 2} or sample_rate not in {16_000, 24_000, 44_100, 48_000}:
        raise AudioDecodeError("UNSUPPORTED_SAMPLE_RATE")
    if declared_sample_rate and declared_sample_rate != sample_rate:
        raise AudioDecodeError("SAMPLE_RATE_MISMATCH")
    if sample_width == 1:
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif sample_width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
    elif sample_width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        values = raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8)
        values |= raw[:, 2].astype(np.int32) << 16
        values = np.where(values & 0x800000, values - 0x1000000, values)
        samples = values.astype(np.float32) / 8_388_608
    elif sample_width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2_147_483_648
    else:
        raise AudioDecodeError("INVALID_AUDIO")
    if not samples.size or samples.size % channels:
        raise AudioDecodeError("INVALID_AUDIO")
    samples = samples.reshape(-1, channels).mean(axis=1)
    return Waveform(np.ascontiguousarray(samples, dtype=np.float32), sample_rate)


def waveform_quality_reason(waveform: Waveform, settings: Settings) -> str | None:
    samples = waveform.samples
    if samples.size / waveform.sample_rate < settings.quality_min_duration_sec:
        return "AUDIO_TOO_SHORT"
    if float(np.mean(np.abs(samples) >= 0.999)) >= settings.quality_max_clipping_ratio:
        return "EXCESSIVE_CLIPPING"
    frame_size = max(1, round(0.05 * waveform.sample_rate))
    usable_size = samples.size - samples.size % frame_size
    frames = samples[:usable_size].reshape(-1, frame_size)
    frame_rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    active_rms = float(np.percentile(frame_rms, settings.quality_active_percentile))
    if 20 * math.log10(active_rms + 1e-12) <= settings.quality_min_active_dbfs:
        return "SIGNAL_TOO_QUIET"
    return None


def resample_16k(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate == 16_000:
        return samples
    import librosa

    return np.asarray(
        librosa.resample(samples, orig_sr=sample_rate, target_sr=16_000, res_type="soxr_hq"),
        dtype=np.float32,
    )


def merge_patch_events(
    candidate_indices: np.ndarray, hop_seconds: float, merge_gap_seconds: float
) -> int:
    if not candidate_indices.size:
        return 0
    count = 1
    previous_time = float(candidate_indices[0]) * hop_seconds
    for index in candidate_indices[1:]:
        current_time = float(index) * hop_seconds
        if current_time - previous_time > merge_gap_seconds:
            count += 1
        previous_time = current_time
    return count


def measured(metric: Metric, value: float, unit: str) -> AcousticMeasurement:
    return AcousticMeasurement(metric, round(float(value), 4), unit, "OK", None)


def unmeasurable(metric: Metric, unit: str, reason: str) -> AcousticMeasurement:
    return AcousticMeasurement(metric, None, unit, "UNMEASURABLE", reason)


def create_acoustic_analyzer(settings: Settings) -> AcousticAnalyzer:
    return CollogAcousticAnalyzer(settings)
