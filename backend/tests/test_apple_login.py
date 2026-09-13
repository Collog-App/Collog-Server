from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AppleLoginChallenge, User
from app.services.apple_auth import AppleIdentityVerifier
from tests.conftest import auth, create_user


@pytest.fixture
def sign_apple(client: TestClient) -> Iterator[Callable[[str, str], str]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="test-apple", alg="RS256", use="sig")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [jwk]}))
    http = httpx.AsyncClient(transport=transport)
    container = client.app.state.container
    client.portal.call(container.apple_identity.close)
    container.apple_identity = AppleIdentityVerifier(container.settings, client=http)

    def sign(nonce: str, subject: str = "apple-user") -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": subject,
                "nonce": nonce,
                "iss": "https://appleid.apple.com",
                "aud": container.settings.apple_client_id,
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            key,
            algorithm="RS256",
            headers={"kid": "test-apple"},
        )

    yield sign
    client.portal.call(http.aclose)


def apple_payload(
    client: TestClient, sign: Callable[[str, str], str], subject: str = "apple-user"
) -> dict:
    response = client.post("/v1/auth/apple/challenge")
    assert response.status_code == 200
    challenge = response.json()
    return {
        "challengeId": challenge["challengeId"],
        "identityToken": sign(challenge["nonce"], subject),
        "role": "CHILD",
        "name": "Apple 사용자",
    }


def test_apple_login_without_phone_and_session_refresh(client: TestClient, sign_apple) -> None:
    response = client.post("/v1/auth/apple", json=apple_payload(client, sign_apple))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user"]["phone"] is None
    assert payload["user"]["appleUserId"] == "apple-user"
    assert payload["user"]["familyId"] is not None
    members = client.get(
        f"/v1/families/{payload['user']['familyId']}/members", headers=auth(payload["accessToken"])
    )
    assert members.status_code == 200
    renewed = client.post("/v1/auth/refresh", json={"refreshToken": payload["refreshToken"]})
    assert renewed.status_code == 200
    assert renewed.json()["user"]["id"] == payload["user"]["id"]


def test_apple_relogin_preserves_name_role_and_family(client: TestClient, sign_apple) -> None:
    first = client.post("/v1/auth/apple", json=apple_payload(client, sign_apple)).json()
    payload = apple_payload(client, sign_apple)
    payload["role"] = "PARENT"
    payload["name"] = None
    second = client.post("/v1/auth/apple", json=payload)
    assert second.status_code == 200
    assert second.json()["user"] == first["user"]


def test_apple_parent_can_accept_existing_sms_family_invitation(
    client: TestClient, sign_apple
) -> None:
    token, child = create_user(client, "01012345678", "CHILD", "자녀")
    invitation = client.post(
        f"/v1/families/{child['familyId']}/invitations",
        headers=auth(token),
        json={"name": "부모", "relation": "MOTHER"},
    )
    payload = apple_payload(client, sign_apple)
    payload["role"] = "PARENT"
    parent = client.post("/v1/auth/apple", json=payload).json()
    assert parent["user"]["familyId"] is None
    accepted = client.post(
        "/v1/invitations/accept",
        headers=auth(parent["accessToken"]),
        json={"code": invitation.json()["code"]},
    )
    assert accepted.status_code == 200
    assert accepted.json()["familyId"] == child["familyId"]


def test_apple_challenge_can_only_be_consumed_once(client: TestClient, sign_apple) -> None:
    payload = apple_payload(client, sign_apple)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda _: client.post("/v1/auth/apple", json=payload), range(2))
        )
    assert sorted(response.status_code for response in responses) == [200, 401]


def test_wrong_nonce_does_not_consume_challenge(client: TestClient, sign_apple) -> None:
    valid = apple_payload(client, sign_apple)
    invalid = {**valid, "identityToken": sign_apple("wrong-nonce", "apple-user")}
    assert client.post("/v1/auth/apple", json=invalid).status_code == 401
    assert client.post("/v1/auth/apple", json=valid).status_code == 200


def test_expired_challenge_is_rejected(client: TestClient, sign_apple) -> None:
    payload = apple_payload(client, sign_apple)

    async def expire() -> None:
        async with client.app.state.container.database.sessions() as session:
            challenge = await session.get(AppleLoginChallenge, payload["challengeId"])
            challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    client.portal.call(expire)
    assert client.post("/v1/auth/apple", json=payload).status_code == 401


def test_sms_account_is_not_merged_with_apple_account(client: TestClient, sign_apple) -> None:
    _, sms = create_user(client, "01012345678", "CHILD", "Apple 사용자")
    apple = client.post("/v1/auth/apple", json=apple_payload(client, sign_apple)).json()["user"]
    assert apple["id"] != sms["id"]

    async def phones() -> list[str | None]:
        async with client.app.state.container.database.sessions() as session:
            return list(await session.scalars(select(User.phone)))

    assert set(client.portal.call(phones)) == {"01012345678", None}


def test_disabled_apple_login_preserves_sms(client: TestClient) -> None:
    client.app.state.container.settings.apple_login_enabled = False
    assert client.post("/v1/auth/apple/challenge").status_code == 503
    _, sms = create_user(client, "01012345678", "CHILD", "사용자")
    assert sms["phone"] == "01012345678"


def test_two_apple_accounts_can_both_have_no_phone(client: TestClient, sign_apple) -> None:
    users = [
        client.post("/v1/auth/apple", json=apple_payload(client, sign_apple, subject)).json()[
            "user"
        ]
        for subject in ("first-apple-user", "second-apple-user")
    ]
    assert users[0]["id"] != users[1]["id"]
    assert all(user["phone"] is None for user in users)
