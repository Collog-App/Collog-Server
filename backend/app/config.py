from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/v1"
    public_base_url: str = "http://localhost:8080"
    database_url: str = "sqlite+aiosqlite:///./.data/collog.db"
    jwt_secret: str = "development-only-change-me-at-least-32-chars"
    jwt_ttl_minutes: int = 60 * 24
    refresh_ttl_days: int = 30
    otp_ttl_seconds: int = 180
    dev_otp_code: str = "000000"
    mock_external_services: bool = False
    sms_provider: Literal["solapi", "mock"] = "solapi"
    solapi_api_key: str = ""
    solapi_api_secret: str = ""
    solapi_sender: str = ""
    otp_max_attempts: int = Field(default=5, ge=1, le=10)
    apple_login_enabled: bool = True
    apple_client_id: str = "com.dohyeoplim.collog-ios"
    apple_challenge_ttl_seconds: int = Field(default=300, ge=30, le=600)

    schema_auto_reset: bool = False

    livekit_url: str = "ws://localhost:7880"
    livekit_internal_url: str | None = None
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    apns_voip_enabled: bool = False
    apns_environment: Literal["sandbox", "production"] = "sandbox"
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_bundle_id: str = ""
    apns_private_key_path: Path | None = None
    incoming_call_ttl_seconds: int = 45

    # Track Egress worker가 없는 개발 환경에서 기기의 분석용 PCM만으로 파이프라인을 돌린다.
    # 부모/자녀 트랙 분리 전사가 없으므로 운영에서는 반드시 false로 둔다.
    allow_raw_only_analysis: bool = False

    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    deepgram_language: str = "ko"
    deepgram_base_url: str = "https://api.deepgram.com"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_max_output_tokens: int = 2048

    question_tts_provider: Literal["ios_local", "elevenlabs", "elevenlabs_direct"] = "ios_local"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_output_format: str = "mp3_44100_128"
    elevenlabs_base_url: str = "https://api.elevenlabs.io"

    storage_backend: Literal["local", "s3"] = "local"
    local_storage_path: Path = Path(".data/audio")
    upload_url_ttl_seconds: int = 900
    tts_url_ttl_seconds: int = 3_600
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_region: str = "ap-northeast-2"
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_force_path_style: bool = True

    consent_document_version: str = "2026-08-01.v3"
    parent_min_speech_seconds: int = 20
    raw_audio_wait_seconds: int = 30
    egress_wait_seconds: int = Field(default=120, ge=1)
    processing_timeout_seconds: int = Field(default=600, ge=1)
    processing_lease_seconds: int = Field(default=900, ge=2)
    maintenance_interval_seconds: int = Field(default=10, ge=1)
    baseline_required_samples: int = 4
    baseline_window_weeks: int = 4
    robust_z_threshold: float = 1.5
    promoted_consecutive_weeks: int = 4
    acoustic_analyzer_version: str = "collog-acoustic-v5"
    cough_score_threshold: float = 0.65

    # transient-heuristic-v1은 라벨 음원에서 재현율이 사실상 0이다. 공개 도메인 기침 녹음
    # 4개에서 최고 점수가 0.609로 임계값 0.65에 닿지 못했고, 임계값을 0.40으로 낮추면 웃음이
    # 7회로 모든 기침 파일보다 높게 잡힌다. 라벨 세트에서 precision을 제시하기 전까지
    # COUGH_EVENTS를 UNMEASURABLE로 고정한다. `0.0 OK`가 저장되면 "기침이 없었다"로 읽히고
    # 기준선에 0만 쌓여 MAD가 0이 되므로, 이후 실제 기침을 이상치로 만들지 못한다.
    cough_detector_validated: bool = False

    # HeAR event detector. 가중치는 HAI-DEF 약관 대상이라 저장소에 없다.
    # `scripts/fetch_cough_model.py`로 받으며, 파일이 없으면 MODEL_UNAVAILABLE로 떨어진다.
    cough_detector: Literal["transient-heuristic-v1", "hear-event-detector-small-v1"] = (
        "hear-event-detector-small-v1"
    )
    cough_model_path: Path = Path("models/hear-event-detector-small.onnx")
    cough_model_sha256: str = "636caf6c4c6ff036e10ea2c745928d9ab546f9227837c7b1986250d5025736fb"
    # Google 데모 기본값. 라벨 음원 8개에서 기침 최소 0.999, hard negative 최대 0.688로
    # 양쪽에 여유가 있으나 통화 조건에서 재측정하기 전까지 검증된 동작점이 아니다.
    cough_detection_threshold: float = 0.9
    # 2초 window를 얼마나 촘촘히 밀지. 짧을수록 구간 경계가 정확해지고 비용이 늘어난다.
    cough_window_hop_seconds: float = 0.5
    # 이보다 짧게 끊긴 검출은 같은 구간으로 본다.
    cough_merge_gap_seconds: float = 0.75

    # 아래 값은 실측으로 보정되지 않은 잠정 상수다. 코드에 숨어 있으면 틀렸다는 사실조차
    # 드러나지 않으므로 설정으로 노출한다. 값을 바꿀 때에는 저장된 값과 구분되도록
    # acoustic_analyzer_version도 함께 올린다.
    quality_min_duration_sec: float = 5.0
    quality_max_clipping_ratio: float = 0.01
    quality_active_percentile: float = 90.0
    quality_min_active_dbfs: float = -55.0
    pause_min_gap_ms: int = 300
    pause_max_gap_ms: int = 2_000

    # STT 결과를 서버 로그로 확인하기 위한 개발용 스위치. transcript 원문은 건강 관련
    # 민감정보이므로 요약 통계만 항상 남기고, 발화 텍스트는 이 값이 true일 때만 남긴다.
    log_stt_transcript: bool = False

    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    team_portal_enabled: bool = True
    team_portal_repository_url: str = "https://github.com/Collog-App/14th-hackathon"

    @property
    def livekit_http_url(self) -> str:
        value = self.livekit_internal_url or self.livekit_url
        if value.startswith("wss://"):
            return "https://" + value.removeprefix("wss://")
        if value.startswith("ws://"):
            return "http://" + value.removeprefix("ws://")
        return value

    def ensure_local_directories(self) -> None:
        if self.database_url.startswith("sqlite"):
            database_path = self.database_url.rsplit("///", 1)[-1]
            if database_path != ":memory:":
                Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        if self.storage_backend == "local":
            self.local_storage_path.mkdir(parents=True, exist_ok=True)

    def validate_runtime(self) -> None:
        if self.processing_lease_seconds <= self.processing_timeout_seconds:
            raise ValueError("PROCESSING_LEASE_SECONDS must exceed PROCESSING_TIMEOUT_SECONDS")
        if self.app_env != "production":
            return
        if self.mock_external_services or self.sms_provider != "solapi":
            raise ValueError("Production requires real external services and SMS")
        if self.schema_auto_reset or self.allow_raw_only_analysis:
            raise ValueError("Production requires migrations and Track Egress")
        if self.storage_backend != "s3" or not self.database_url.startswith(
            "postgresql+asyncpg://"
        ):
            raise ValueError("Production requires PostgreSQL and S3 storage")
        if not self.public_base_url.startswith("https://") or not self.livekit_url.startswith(
            "wss://"
        ):
            raise ValueError("Production requires HTTPS and WSS public URLs")
        if not (self.s3_public_endpoint_url or "").startswith("https://"):
            raise ValueError("Production requires an HTTPS storage URL")
        required = {
            "JWT_SECRET": self.jwt_secret,
            "LIVEKIT_API_KEY": self.livekit_api_key,
            "LIVEKIT_API_SECRET": self.livekit_api_secret,
            "S3_BUCKET": self.s3_bucket,
            "S3_ACCESS_KEY_ID": self.s3_access_key_id,
            "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
            "DEEPGRAM_API_KEY": self.deepgram_api_key,
            "GEMINI_API_KEY": self.gemini_api_key,
            "APNS_TEAM_ID": self.apns_team_id,
            "APNS_KEY_ID": self.apns_key_id,
            "APNS_BUNDLE_ID": self.apns_bundle_id,
        }
        if self.apple_login_enabled:
            required["APPLE_CLIENT_ID"] = self.apple_client_id
        sms_settings = {
            "SOLAPI_API_KEY": self.solapi_api_key,
            "SOLAPI_API_SECRET": self.solapi_api_secret,
            "SOLAPI_SENDER": self.solapi_sender,
        }
        if not self.apple_login_enabled or any(sms_settings.values()):
            required.update(sms_settings)
        missing = [key for key, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Missing production settings: {', '.join(missing)}")
        for name, value in (
            ("JWT_SECRET", self.jwt_secret),
            ("LIVEKIT_API_SECRET", self.livekit_api_secret),
            ("S3_SECRET_ACCESS_KEY", self.s3_secret_access_key),
        ):
            if len(value) < 32 or any(
                word in value.lower() for word in ("development", "local", "change")
            ):
                raise ValueError(f"{name} must be a unique secret of at least 32 characters")
        if not self.apns_voip_enabled or self.apns_private_key_path is None:
            raise ValueError("Production requires APNs VoIP credentials")
        if not self.apns_private_key_path.is_file():
            raise ValueError("APNS_PRIVATE_KEY_PATH must point to a readable key file")
        if self.question_tts_provider in {"elevenlabs", "elevenlabs_direct"} and not (
            self.elevenlabs_api_key and self.elevenlabs_voice_id
        ):
            raise ValueError("ElevenLabs requires an API key and voice ID")


@lru_cache
def get_settings() -> Settings:
    return Settings()
