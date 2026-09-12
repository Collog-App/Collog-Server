from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app import auth_router
from app.config import Settings
from app.services.sms import SmsDeliveryError, send_otp
from tests.conftest import auth


def login(client: TestClient, phone: str = "01012345678") -> dict:
    requested = client.post("/v1/auth/otp/request", json={"phone": phone})
    assert requested.status_code == 202
    verified = client.post(
        "/v1/auth/otp/verify", json={"phone": phone, "code": requested.json()["devCode"]}
    )
    assert verified.status_code == 200
    return verified.json()


def test_otp_attempt_limit_and_resend(client: TestClient) -> None:
    requested = client.post("/v1/auth/otp/request", json={"phone": "01012345678"})
    for _ in range(client.app.state.container.settings.otp_max_attempts):
        response = client.post(
            "/v1/auth/otp/verify", json={"phone": "01012345678", "code": "999999"}
        )
        assert response.status_code == 401
    response = client.post(
        "/v1/auth/otp/verify", json={"phone": "01012345678", "code": requested.json()["devCode"]}
    )
    assert response.status_code == 401
    assert login(client)["user"]["phone"] == "01012345678"


def test_latest_otp_is_single_use_and_older_code_stays_invalid(client: TestClient) -> None:
    settings = client.app.state.container.settings
    settings.dev_otp_code = "111111"
    client.post("/v1/auth/otp/request", json={"phone": "01012345678"})
    settings.dev_otp_code = "222222"
    login(client)
    for code in ("111111", "222222"):
        response = client.post("/v1/auth/otp/verify", json={"phone": "01012345678", "code": code})
        assert response.status_code == 401


def test_concurrent_otp_verification_only_issues_one_session(client: TestClient) -> None:
    response = client.post("/v1/auth/otp/request", json={"phone": "01012345678"})
    payload = {"phone": "01012345678", "code": response.json()["devCode"]}

    def verify(_: int) -> int:
        return client.post("/v1/auth/otp/verify", json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(verify, range(2)))
    assert sorted(statuses) == [200, 401]


def test_expired_otp_is_rejected(client: TestClient) -> None:
    client.app.state.container.settings.otp_ttl_seconds = -1
    requested = client.post("/v1/auth/otp/request", json={"phone": "01012345678"})
    response = client.post(
        "/v1/auth/otp/verify", json={"phone": "01012345678", "code": requested.json()["devCode"]}
    )
    assert response.status_code == 401


def test_phone_normalization_shares_account_and_rate_limit(client: TestClient) -> None:
    first = login(client, "010-1234-5678")
    second = login(client, "+82 10 1234 5678")
    assert first["user"]["id"] == second["user"]["id"]
    for _ in range(3):
        assert client.post("/v1/auth/otp/request", json={"phone": "01012345678"}).status_code == 202
    response = client.post("/v1/auth/otp/request", json={"phone": "010-1234-5678"})
    assert response.status_code == 429


def test_failed_sms_does_not_enable_verification(client: TestClient, monkeypatch) -> None:
    async def fail(settings: Settings, phone: str, code: str) -> None:
        raise SmsDeliveryError("문자 발송 실패")

    monkeypatch.setattr(auth_router, "send_otp", fail)
    response = client.post("/v1/auth/otp/request", json={"phone": "01012345678"})
    assert response.status_code == 503
    response = client.post("/v1/auth/otp/verify", json={"phone": "01012345678", "code": "000000"})
    assert response.status_code == 401


def test_development_uses_random_sms_code_without_exposing_it(
    client: TestClient, monkeypatch
) -> None:
    settings = client.app.state.container.settings
    settings.app_env = "development"
    delivered: list[str] = []

    async def capture(settings: Settings, phone: str, code: str) -> None:
        delivered.append(code)

    monkeypatch.setattr(auth_router, "send_otp", capture)
    monkeypatch.setattr(auth_router.secrets, "randbelow", lambda upper: 123456)
    response = client.post("/v1/auth/otp/request", json={"phone": "01012345678"})
    assert response.status_code == 202
    assert "devCode" not in response.json()
    assert delivered == ["123456"]


def test_refresh_rotates_and_logout_revokes_access(client: TestClient) -> None:
    initial = login(client)
    response = client.post("/v1/auth/refresh", json={"refreshToken": initial["refreshToken"]})
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refreshToken"] != initial["refreshToken"]
    assert rotated["user"] == initial["user"]
    assert client.post(
        "/v1/auth/refresh", json={"refreshToken": initial["refreshToken"]}
    ).status_code == 401
    family_url = f"/v1/families/{initial['user']['familyId']}/members"
    assert client.get(family_url, headers=auth(rotated["accessToken"])).status_code == 200
    assert client.post(
        "/v1/auth/logout", json={"refreshToken": rotated["refreshToken"]}
    ).status_code == 204
    assert client.post(
        "/v1/auth/refresh", json={"refreshToken": rotated["refreshToken"]}
    ).status_code == 401
    for token in (initial["accessToken"], rotated["accessToken"]):
        assert client.get(family_url, headers=auth(token)).status_code == 401


def test_refresh_works_after_access_expiration(client: TestClient) -> None:
    initial = login(client)
    settings = client.app.state.container.settings
    claims = jwt.decode(initial["accessToken"], settings.jwt_secret, algorithms=["HS256"])
    claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    expired = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    family_url = f"/v1/families/{initial['user']['familyId']}/members"
    assert client.get(family_url, headers=auth(expired)).status_code == 401
    response = client.post("/v1/auth/refresh", json={"refreshToken": initial["refreshToken"]})
    assert response.status_code == 200
    assert client.get(family_url, headers=auth(response.json()["accessToken"])).status_code == 200


def test_concurrent_refresh_only_rotates_once(client: TestClient) -> None:
    initial = login(client)

    def refresh(_: int) -> int:
        return client.post(
            "/v1/auth/refresh", json={"refreshToken": initial["refreshToken"]}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(refresh, range(2)))
    assert sorted(statuses) == [200, 401]


@pytest.mark.asyncio
@pytest.mark.parametrize("registered_success", [0, 1])
async def test_solapi_signs_request_and_checks_registration(
    monkeypatch, registered_success: int
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        solapi_api_key="test-key",
        solapi_api_secret="test-secret",
        solapi_sender="0212345678",
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.solapi.com/messages/v4/send-many/detail"
        fields = dict(
            part.split("=", 1) for part in request.headers["Authorization"][12:].split(", ")
        )
        expected = hmac.new(
            b"test-secret", (fields["date"] + fields["salt"]).encode(), hashlib.sha256
        ).hexdigest()
        assert fields["signature"] == expected
        assert fields["apiKey"] == "test-key"
        message = json.loads(request.content)["messages"][0]
        assert message["to"] == "01012345678"
        assert message["from"] == settings.solapi_sender
        assert "123456" in message["text"]
        return httpx.Response(200, json={"groupInfo": {"count": {
            "registeredSuccess": registered_success, "registeredFailed": 1 - registered_success,
        }}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs
    ))
    if registered_success:
        await send_otp(settings, "01012345678", "123456")
    else:
        with pytest.raises(SmsDeliveryError):
            await send_otp(settings, "01012345678", "123456")


@pytest.mark.asyncio
async def test_sms_requires_real_configuration_outside_tests() -> None:
    settings = Settings(_env_file=None, app_env="development", mock_external_services=True)
    with pytest.raises(SmsDeliveryError):
        await send_otp(settings, "01012345678", "123456")
