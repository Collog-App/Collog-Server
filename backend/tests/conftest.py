from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Unit/API tests must not accidentally instantiate real providers just because a
# developer's ignored .env has MOCK_EXTERNAL_SERVICES=false.
os.environ["MOCK_EXTERNAL_SERVICES"] = "true"

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        app_env="test",
        schema_auto_reset=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        local_storage_path=tmp_path / "audio",
        public_base_url="http://testserver",
        jwt_secret="test-secret-with-more-than-thirty-two-characters",
        mock_external_services=True,
        raw_audio_wait_seconds=0,
        # 개발자의 .env가 s3나 Egress 없는 분석을 켜두었더라도 테스트는 항상 같은 조건에서
        # 돈다. 명시한 값이 .env보다 우선한다.
        storage_backend="local",
        allow_raw_only_analysis=False,
        question_tts_provider="ios_local",
        elevenlabs_api_key="",
        elevenlabs_voice_id="",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def create_user(client: TestClient, phone: str, role: str, name: str) -> tuple[str, dict]:
    requested = client.post(
        "/v1/auth/otp/request",
        json={"phone": phone, "role": role, "name": name},
    )
    assert requested.status_code == 202, requested.text
    verified = client.post(
        "/v1/auth/otp/verify",
        json={"phone": phone, "code": requested.json()["devCode"]},
    )
    assert verified.status_code == 200, verified.text
    payload = verified.json()
    return payload["accessToken"], payload["user"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
