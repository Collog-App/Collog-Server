from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class UserRole(StrEnum):
    CHILD = "CHILD"
    PARENT = "PARENT"


class MemberRelation(StrEnum):
    MOTHER = "MOTHER"
    FATHER = "FATHER"


class ConsentDecision(StrEnum):
    GRANTED = "GRANTED"
    DENIED = "DENIED"


class CallState(StrEnum):
    CREATED = "CREATED"
    RINGING = "RINGING"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    PROCESSING = "PROCESSING"
    ANALYZED = "ANALYZED"
    ANALYSIS_EXCLUDED = "ANALYSIS_EXCLUDED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"


class TimeSlot(StrEnum):
    MORNING = "MORNING"
    AFTERNOON_EVENING = "AFTERNOON_EVENING"


class AssetKind(StrEnum):
    DEVICE_RAW = "DEVICE_RAW"
    WEBRTC_EGRESS_PARENT = "WEBRTC_EGRESS_PARENT"
    WEBRTC_EGRESS_CHILD = "WEBRTC_EGRESS_CHILD"


class AssetStatus(StrEnum):
    PENDING = "PENDING"
    UPLOADED = "UPLOADED"
    PURGED = "PURGED"
    FAILED = "FAILED"


class Metric(StrEnum):
    COUGH_EVENTS = "COUGH_EVENTS"
    SPEECH_RATE = "SPEECH_RATE"
    PAUSE_RATIO = "PAUSE_RATIO"
    F0_VARIATION = "F0_VARIATION"


class BaselineKind(StrEnum):
    ANCHOR = "ANCHOR"
    ROLLING = "ROLLING"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    role: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    requested_role: Mapped[str] = mapped_column(String(16), default=UserRole.CHILD.value)
    requested_name: Mapped[str] = mapped_column(String(80), default="사용자")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    auth_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("refresh_sessions.id"), nullable=True, index=True
    )
    platform: Mapped[str] = mapped_column(String(16))
    token: Mapped[str] = mapped_column(Text)
    voip_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    push_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Family(Base):
    __tablename__ = "families"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FamilyMember(Base):
    __tablename__ = "family_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    relation: Mapped[str] = mapped_column(String(24))
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    member_id: Mapped[str] = mapped_column(ForeignKey("family_members.id"), index=True)
    code: Mapped[str] = mapped_column(String(6), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsentRecord(Base):
    __tablename__ = "consent_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    document_version: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(16))
    agreed_items: Mapped[list[str]] = mapped_column(JSON, default=list)
    agreed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ParentProfile(Base):
    __tablename__ = "parent_profiles"

    parent_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CallRecord(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    child_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default=CallState.CREATED.value, index=True)
    room_name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_disabled_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_slot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_speech_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asked_question_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_audio_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    room_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AudioAsset(Base):
    __tablename__ = "audio_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    uri: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(80), default="audio/ogg")
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=AssetStatus.PENDING.value)
    egress_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    parent_speech_sec: Mapped[int] = mapped_column(Integer, default=0)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RepeatEvent(Base):
    __tablename__ = "repeat_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    speaker: Mapped[str] = mapped_column(String(16), default=UserRole.PARENT.value)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(32))
    matched_text: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    rule_version: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HealthExtraction(Base):
    __tablename__ = "health_extractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), unique=True, index=True)
    parse_status: Mapped[str] = mapped_column(String(16))
    symptom: Mapped[str | None] = mapped_column(Text, nullable=True)
    medication: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity: Mapped[str | None] = mapped_column(Text, nullable=True)
    sleep: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionEvidence(Base):
    __tablename__ = "extraction_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), unique=True, index=True)
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    schema_version: Mapped[str] = mapped_column(String(24), default="v2")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AcousticFeature(Base):
    __tablename__ = "acoustic_features"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    audio_source: Mapped[str] = mapped_column(String(24))
    metric: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    unmeasurable_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AcousticAnalysisRun(Base):
    __tablename__ = "acoustic_analysis_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), unique=True, index=True)
    analyzer_version: Mapped[str] = mapped_column(String(48))
    cough_detector_version: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Baseline(Base):
    __tablename__ = "baselines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    metric: Mapped[str] = mapped_column(String(32), index=True)
    time_slot: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    sample_count: Mapped[int] = mapped_column(Integer)
    required_count: Mapped[int] = mapped_column(Integer)
    median: Mapped[float | None] = mapped_column(Float, nullable=True)
    mad: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_from: Mapped[date] = mapped_column(Date)
    window_to: Mapped[date] = mapped_column(Date)
    anchor_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChangeSignal(Base):
    __tablename__ = "change_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    metric: Mapped[str] = mapped_column(String(32))
    time_slot: Mapped[str] = mapped_column(String(32))
    vs_anchor: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    vs_rolling: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    consecutive_weeks: Mapped[int] = mapped_column(Integer, default=0)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False)
    acute: Mapped[bool] = mapped_column(Boolean, default=False)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    acute_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    period: Mapped[str] = mapped_column(String(16))
    from_date: Mapped[date] = mapped_column(Date)
    to_date: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
