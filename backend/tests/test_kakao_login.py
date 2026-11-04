from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.services.kakao_auth import KakaoIdentityVerifier
from tests.conftest import auth

APP_KEY = "test-kakao-native-app-key"


@pytest.fixture
def sign_kakao(client: TestClient) -> Iterator[Callable[..., str]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="test-kakao", alg="RS256", use="sig")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [jwk]}))
    http = httpx.AsyncClient(transport=transport)
    container = client.app.state.container
    container.settings.kakao_app_key = APP_KEY
    client.portal.call(container.kakao_identity.close)
    container.kakao_identity = KakaoIdentityVerifier(container.settings, client=http)

    def sign(nonce: str, subject: str = "kakao-user", **claims: object) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": subject,
                "nonce": nonce,
                "iss": "https://kauth.kakao.com",
                "aud": APP_KEY,
                "iat": now,
                "exp": now + timedelta(minutes=5),
                **claims,
            },
            key,
            algorithm="RS256",
            headers={"kid": "test-kakao"},
        )

    yield sign
    client.portal.call(http.aclose)


def kakao_payload(client: TestClient, sign: Callable[..., str], **claims: object) -> dict:
    response = client.post("/v1/auth/kakao/challenge")
    assert response.status_code == 200, response.text
    challenge = response.json()
    return {"challengeId": challenge["challengeId"], "idToken": sign(challenge["nonce"], **claims)}


def test_kakao_login_creates_an_account_without_a_family(client: TestClient, sign_kakao) -> None:
    payload = kakao_payload(client, sign_kakao, nickname="카카오친구", email="friend@kakao.com")
    response = client.post("/v1/auth/kakao", json=payload)
    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert user["name"] == "카카오친구"
    assert user["email"] == "friend@kakao.com"
    assert user["familyId"] is None
    assert user["signupCompleted"] is False

    again = client.post("/v1/auth/kakao", json=kakao_payload(client, sign_kakao))
    assert again.status_code == 200, again.text
    assert again.json()["user"]["id"] == user["id"]


def test_kakao_challenge_is_single_use_and_bound_to_the_nonce(
    client: TestClient, sign_kakao
) -> None:
    payload = kakao_payload(client, sign_kakao)
    assert client.post("/v1/auth/kakao", json=payload).status_code == 200
    assert client.post("/v1/auth/kakao", json=payload).status_code == 401

    challenge = client.post("/v1/auth/kakao/challenge").json()
    wrong = {"challengeId": challenge["challengeId"], "idToken": sign_kakao("other-nonce")}
    assert client.post("/v1/auth/kakao", json=wrong).status_code == 401


def test_kakao_token_for_another_app_is_rejected(client: TestClient, sign_kakao) -> None:
    challenge = client.post("/v1/auth/kakao/challenge").json()
    other_app = sign_kakao(challenge["nonce"], aud="someone-elses-app")
    response = client.post(
        "/v1/auth/kakao", json={"challengeId": challenge["challengeId"], "idToken": other_app}
    )
    assert response.status_code == 401


def test_google_challenge_is_not_accepted_for_kakao(client: TestClient, sign_kakao) -> None:
    client.app.state.container.settings.google_client_id = "test-web-client"
    google = client.post("/v1/auth/google/challenge").json()
    payload = {"challengeId": google["challengeId"], "idToken": sign_kakao(google["nonce"])}
    assert client.post("/v1/auth/kakao", json=payload).status_code == 401


def test_unconfigured_kakao_login_is_unavailable(client: TestClient) -> None:
    assert client.post("/v1/auth/kakao/challenge").status_code == 503
    payload = {"challengeId": "0" * 36, "idToken": "token"}
    assert client.post("/v1/auth/kakao", json=payload).status_code == 503


def test_kakao_account_can_sign_up_and_be_deleted(client: TestClient, sign_kakao) -> None:
    login = client.post("/v1/auth/kakao", json=kakao_payload(client, sign_kakao)).json()
    token = login["accessToken"]
    profile = client.patch(
        "/v1/account/profile",
        headers=auth(token),
        json={"nickname": "카카오가족", "gender": "MALE", "birthDate": "1990-01-01"},
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["signupCompleted"] is True
    assert client.delete("/v1/account", headers=auth(token)).status_code == 204
