from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from app.models import BaselineKind, CallState, Metric, TimeSlot, UserRole


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        use_enum_values=True,
    )


class ErrorResponse(ApiModel):
    code: str
    message: str


def normalize_phone(value: object) -> object:
    if not isinstance(value, str):
        return value
    phone = value.replace("-", "").replace(" ", "")
    if phone.startswith("+82"):
        phone = "0" + phone[3:]
    return phone


PhoneNumber = Annotated[
    str, Field(pattern=r"^01[016789][0-9]{7,8}$"), BeforeValidator(normalize_phone)
]


class OtpRequest(ApiModel):
    phone: PhoneNumber
    role: UserRole = UserRole.CHILD
    name: str = Field(default="사용자", min_length=1, max_length=80)


class OtpVerify(ApiModel):
    phone: PhoneNumber
    code: str = Field(pattern=r"^[0-9]{6}$")


class RefreshRequest(ApiModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class AppleLoginRequest(ApiModel):
    identity_token: str = Field(min_length=1, max_length=16_384)
    challenge_id: str = Field(min_length=36, max_length=36)
    role: UserRole = UserRole.CHILD
    name: str | None = Field(default=None, max_length=80)


class AppleChallengeResponse(ApiModel):
    challenge_id: str
    nonce: str
    expires_in: int


class UserView(ApiModel):
    id: str
    role: str
    name: str
    phone: str | None = None
    apple_user_id: str | None = None
    family_id: str | None = None


class TokenResponse(ApiModel):
    access_token: str
    refresh_token: str
    user: UserView


class DeviceCreate(ApiModel):
    platform: Literal["IOS", "ANDROID"]
    token: str
    voip_token: str | None = None
    call_notifications_enabled: bool = True
    push_token: str | None = None
    report_notifications_enabled: bool = True


class InvitationCreate(ApiModel):
    name: str = Field(min_length=1, max_length=80)
    relation: Literal["MOTHER", "FATHER"]


class InvitationAccept(ApiModel):
    code: str = Field(pattern=r"^[0-9]{6}$")


class ConsentSubmit(ApiModel):
    document_version: str
    decision: Literal["GRANT", "DENY"]
    scrolled_to_end: bool
    agreed_items: list[str]


class ProfilePut(ApiModel):
    conditions: list[Literal["DIABETES", "HYPERTENSION", "DYSLIPIDEMIA", "ASTHMA", "OBESITY"]]

    @field_validator("conditions")
    @classmethod
    def conditions_must_not_be_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("관리가 필요한 질환을 1개 이상 선택해주세요")
        return list(dict.fromkeys(value))


class QuestionView(ApiModel):
    question_id: str
    text: str
    condition_code: str | None
    tts_asset_url: str | None
    duration_ms: int | None
    tts_mode: Literal["IOS_LOCAL", "REMOTE_ASSET"] = "IOS_LOCAL"


class AudioConstraints(ApiModel):
    echo_cancellation: bool = True
    noise_suppression: bool = False
    auto_gain_control: bool = False
    dtx: bool = False
    audio_bitrate: int = 48_000
    raw_capture_sample_rate: int = 16_000


class CallCreate(ApiModel):
    callee_id: str


class CallCreated(ApiModel):
    call_id: str
    caller_id: str
    callee_id: str
    raw_capture_required: bool
    livekit_url: str
    room_name: str
    access_token: str
    recording_enabled: bool
    recording_disabled_reason: str | None = None
    recording_disabled_message: str | None = None
    questions: list[QuestionView]
    audio_constraints: AudioConstraints


class CallAccepted(ApiModel):
    call_id: str
    livekit_url: str
    room_name: str
    access_token: str
    raw_capture_required: bool
    audio_constraints: AudioConstraints


class RawAudioUploadRequest(ApiModel):
    content_type: str
    duration_sec: float
    sample_rate: int


class RawAudioComplete(ApiModel):
    asset_id: str


class CallView(ApiModel):
    call_id: str
    parent_id: str
    child_id: str
    state: CallState | str
    time_slot: TimeSlot | str | None
    started_at: datetime
    ended_at: datetime | None
    duration_sec: int | None
    recorded: bool
    parent_speech_sec: int | None
    asked_question_ids: list[str]
    raw_audio_purged_at: datetime | None


class TranscriptSegment(ApiModel):
    segment_id: str
    speaker: Literal["PARENT", "CHILD"]
    start_ms: int
    end_ms: int
    text: str
    words: list[dict[str, Any]] = Field(default_factory=list)


class RepeatEventView(ApiModel):
    start_ms: int
    end_ms: int
    category: Literal["REPEAT_REQUEST", "HEARING_DIFFICULTY", "CLARIFICATION"]
    matched_text: str
    rule_id: str
    confidence: float
    rule_version: str


class TranscriptView(ApiModel):
    call_id: str
    provider: str
    excluded: bool
    exclusion_reason: str | None
    parent_speech_sec: int
    segments: list[TranscriptSegment]
    repeat_events: list[RepeatEventView] = Field(default_factory=list)
    repeat_request_count: int = 0
    repeat_requests_per_minute: float = 0


class ExtractionFact(ApiModel):
    category: Literal["symptom", "medication", "activity", "sleep"] = Field(
        description="허용된 건강 대화 범주"
    )
    summary: str = Field(min_length=1, max_length=240, description="부모 진술의 중립적 요약")
    polarity: Literal["PRESENT", "ABSENT", "UNCERTAIN"]
    evidence_segment_ids: list[str] = Field(min_length=1, description="근거가 된 PARENT segment ID")


class ExtractionPayload(ApiModel):
    symptom: str | None = Field(default=None, description="관찰되거나 직접 언급된 증상")
    medication: str | None = Field(default=None, description="복약 여부나 변화")
    activity: str | None = Field(default=None, description="활동량이나 일상 활동")
    sleep: str | None = Field(default=None, description="수면 상태나 변화")
    facts: list[ExtractionFact] = Field(default_factory=list)


class GeminiExtractionResponse(ApiModel):
    facts: list[ExtractionFact] = Field(
        default_factory=list,
        max_length=4,
        description="범주별 최대 한 건, 근거가 없으면 빈 배열",
    )


class ExtractionView(ExtractionPayload):
    call_id: str
    parse_status: Literal["OK", "FAILED"]
    raw_transcript: str | None = None


class AcousticFeatureView(ApiModel):
    metric: Metric | str
    value: float | None
    unit: str
    status: Literal["OK", "UNMEASURABLE"]
    unmeasurable_reason: str | None


class ComparisonView(ApiModel):
    delta_pct: float
    direction: Literal["UP", "DOWN", "STABLE"]
    robust_z: float
    significant: bool


class BaselineView(ApiModel):
    parent_id: str
    metric: Metric | str
    time_slot: TimeSlot | str
    kind: BaselineKind | str
    status: Literal["COLLECTING", "READY", "UNSCORABLE"]
    sample_count: int
    required_count: int
    remaining_calls: int | None
    median: float | None
    mad: float | None
    window_from: date
    window_to: date
    anchor_set_at: datetime | None
    anchor_age_weeks: int | None
    computed_at: datetime


class ChangeSignalView(ApiModel):
    signal_id: str
    metric: Metric | str
    time_slot: TimeSlot | str
    vs_anchor: ComparisonView | None
    vs_rolling: ComparisonView | None
    consecutive_weeks: int
    promoted: bool
    acute: bool
    summary_text: str | None
    acute_text: str | None
    observed_at: datetime


class ReportView(ApiModel):
    parent_id: str
    period: Literal["WEEKLY", "MONTHLY"]
    from_: date = Field(alias="from")
    to: date
    state: Literal["READY", "EMPTY", "BASELINE_COLLECTING"]
    empty_message: str | None = None
    disclaimer: str
    advisory: str | None = None
    promoted_signals: list[dict[str, Any]]
    acute_signals: list[dict[str, Any]]
    conversation_items: dict[str, list[str]]
    repeat_observation: dict[str, Any]
    acoustic_trends: list[dict[str, Any]]
    analyzed_call_count: int
    issued_at: datetime
