from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings


@pytest.fixture
def production(tmp_path: Path) -> Settings:
    key = tmp_path / "apns.p8"
    key.write_text("validation fixture")
    return Settings(
        _env_file=None,
        app_env="production",
        mock_external_services=False,
        database_url="postgresql+asyncpg://user:password@postgres/collog",
        storage_backend="s3",
        public_base_url="https://api.example.com",
        livekit_url="wss://rtc.example.com",
        s3_public_endpoint_url="https://storage.example.com",
        jwt_secret="a" * 48,
        livekit_api_key="key",
        livekit_api_secret="b" * 48,
        s3_bucket="audio",
        s3_access_key_id="access",
        s3_secret_access_key="c" * 48,
        deepgram_api_key="deepgram",
        gemini_api_key="gemini",
        solapi_api_key="solapi",
        solapi_api_secret="sms-secret",
        solapi_sender="01012345678",
        apns_voip_enabled=True,
        apns_team_id="team",
        apns_key_id="key",
        apns_bundle_id="com.example.app",
        apns_private_key_path=key,
    )


def test_production_requires_real_services(production: Settings) -> None:
    production.validate_runtime()
    production.mock_external_services = True
    with pytest.raises(ValueError, match="real external"):
        production.validate_runtime()


def test_production_refuses_destructive_schema_reset(production: Settings) -> None:
    production.schema_auto_reset = True
    with pytest.raises(ValueError, match="migrations"):
        production.validate_runtime()


@pytest.mark.parametrize("field", ["deepgram_api_key", "solapi_api_key", "apns_key_id"])
def test_production_requires_provider_credentials(production: Settings, field: str) -> None:
    setattr(production, field, "")
    with pytest.raises(ValueError, match="Missing production"):
        production.validate_runtime()


def test_production_refuses_plaintext_public_urls(production: Settings) -> None:
    production.livekit_url = "ws://rtc.example.com"
    with pytest.raises(ValueError, match="HTTPS and WSS"):
        production.validate_runtime()


def test_apple_login_can_run_without_sms_credentials(production: Settings) -> None:
    production.solapi_api_key = ""
    production.solapi_api_secret = ""
    production.solapi_sender = ""
    production.validate_runtime()
    production.apple_login_enabled = False
    with pytest.raises(ValueError, match="SOLAPI_API_KEY"):
        production.validate_runtime()


def test_enabled_apple_login_requires_client_id(production: Settings) -> None:
    production.apple_client_id = ""
    with pytest.raises(ValueError, match="APPLE_CLIENT_ID"):
        production.validate_runtime()


def test_production_accepts_instance_role_without_static_keys(production: Settings) -> None:
    production.s3_use_instance_role = True
    production.s3_access_key_id = ""
    production.s3_secret_access_key = ""
    production.validate_runtime()


def test_production_requires_explicit_instance_role_mode(production: Settings) -> None:
    production.s3_access_key_id = ""
    production.s3_secret_access_key = ""
    with pytest.raises(ValueError, match="S3_ACCESS_KEY_ID"):
        production.validate_runtime()


def test_instance_role_refuses_static_keys(production: Settings) -> None:
    production.s3_use_instance_role = True
    with pytest.raises(ValueError, match="without static S3 keys"):
        production.validate_runtime()


def test_launch_setting_reads_empty_and_naive_values() -> None:
    # 빈 값은 결제 꺼짐, 시간대 없는 값은 한국 시간.
    assert Settings(billing_launch_at="").billing_launch_at is None
    assert Settings(billing_launch_at="2026-12-15T00:00:00").billing_launch_at == datetime(
        2026, 12, 15, tzinfo=ZoneInfo("Asia/Seoul")
    )
    assert Settings(billing_launch_at="2026-12-15T00:00:00+00:00").billing_launch_at == datetime(
        2026, 12, 15, tzinfo=UTC
    )


def test_production_does_not_require_billing_settings(production: Settings) -> None:
    # 결제 값이 하나도 없어도 운영 기동을 막지 않는다(결제가 꺼진 채로 동작).
    assert production.billing_launch_at is None
    assert production.revenuecat_secret_key == production.revenuecat_webhook_auth == ""
    production.validate_runtime()
