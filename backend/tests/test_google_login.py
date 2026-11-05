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

from app.models import GoogleLoginChallenge, User
from app.services.google_auth import GoogleIdentityVerifier
from tests.conftest import auth, create_user

CLIENT_ID = "test-web-client.apps.googleusercontent.com"


@pytest.fixture
def sign_google(client: TestClient) -> Iterator[Callable[..., str]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="test-google", alg="RS256", use="sig")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [jwk]}))
    http = httpx.AsyncClient(transport=transport)
    container = client.app.state.container
    container.settings.google_client_id = CLIENT_ID
    client.portal.call(container.google_identity.close)
    container.google_identity = GoogleIdentityVerifier(container.settings, client=http)

    def sign(nonce: str, subject: str = "google-user", **claims: object) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": subject,
                "nonce": nonce,
                "iss": "https://accounts.google.com",
                "aud": CLIENT_ID,
                "iat": now,
                "exp": now + timedelta(minutes=5),
                **claims,
            },
            key,
            algorithm="RS256",
            headers={"kid": "test-google"},
        )

    yield sign
    client.portal.call(http.aclose)


def google_payload(
    client: TestClient, sign: Callable[[str, str], str], subject: str = "google-user"
) -> dict:
    response = client.post("/v1/auth/google/challenge")
    assert response.status_code == 200, response.text
    challenge = response.json()
    return {
        "challengeId": challenge["challengeId"],
        "idToken": sign(challenge["nonce"], subject),
        "role": "CHILD",
        "name": "Google 사용자",
    }


def test_google_login_without_phone_and_session_refresh(client: TestClient, sign_google) -> None:
    response = client.post("/v1/auth/google", json=google_payload(client, sign_google))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user"]["phone"] is None
    assert payload["user"]["appleUserId"] is None
    assert payload["user"]["name"] == "Google 사용자"
    assert payload["user"]["familyId"] is not None
    renewed = client.post("/v1/auth/refresh", json={"refreshToken": payload["refreshToken"]})
    assert renewed.status_code == 200
    assert renewed.json()["user"]["id"] == payload["user"]["id"]


def test_google_relogin_preserves_name_role_and_family(client: TestClient, sign_google) -> None:
    first = client.post("/v1/auth/google", json=google_payload(client, sign_google)).json()
    payload = google_payload(client, sign_google)
    payload["role"] = "PARENT"
    payload["name"] = None
    second = client.post("/v1/auth/google", json=payload)
    assert second.status_code == 200
    assert second.json()["user"] == first["user"]


def test_google_parent_can_accept_existing_family_invitation(
    client: TestClient, sign_google
) -> None:
    token, child = create_user(client, "01012345678", "CHILD", "자녀")
    invitation = client.post(
        f"/v1/families/{child['familyId']}/invitations",
        headers=auth(token),
        json={"name": "부모", "relation": "MOTHER"},
    )
    payload = google_payload(client, sign_google)
    payload["role"] = "PARENT"
    parent = client.post("/v1/auth/google", json=payload).json()
    assert parent["user"]["familyId"] is None
    accepted = client.post(
        "/v1/invitations/accept",
        headers=auth(parent["accessToken"]),
        json={"code": invitation.json()["code"]},
    )
    assert accepted.status_code == 200
    assert accepted.json()["familyId"] == child["familyId"]


def test_google_challenge_can_only_be_consumed_once(client: TestClient, sign_google) -> None:
    payload = google_payload(client, sign_google)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda _: client.post("/v1/auth/google", json=payload), range(2))
        )
    assert sorted(response.status_code for response in responses) == [200, 401]


def test_wrong_nonce_does_not_consume_challenge(client: TestClient, sign_google) -> None:
    valid = google_payload(client, sign_google)
    invalid = {**valid, "idToken": sign_google("wrong-nonce", "google-user")}
    assert client.post("/v1/auth/google", json=invalid).status_code == 401
    assert client.post("/v1/auth/google", json=valid).status_code == 200


def test_expired_challenge_is_rejected(client: TestClient, sign_google) -> None:
    payload = google_payload(client, sign_google)

    async def expire() -> None:
        async with client.app.state.container.database.sessions() as session:
            challenge = await session.get(GoogleLoginChallenge, payload["challengeId"])
            challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    client.portal.call(expire)
    assert client.post("/v1/auth/google", json=payload).status_code == 401


def test_apple_challenge_is_not_accepted_for_google(client: TestClient, sign_google) -> None:
    apple = client.post("/v1/auth/apple/challenge").json()
    payload = {
        "challengeId": apple["challengeId"],
        "idToken": sign_google(apple["nonce"]),
        "role": "CHILD",
    }
    assert client.post("/v1/auth/google", json=payload).status_code == 401


def test_sms_account_is_not_merged_with_google_account(client: TestClient, sign_google) -> None:
    _, sms = create_user(client, "01012345678", "CHILD", "Google 사용자")
    google = client.post("/v1/auth/google", json=google_payload(client, sign_google)).json()
    assert google["user"]["id"] != sms["id"]

    async def subjects() -> set[tuple[str | None, str | None]]:
        async with client.app.state.container.database.sessions() as session:
            return set((await session.execute(select(User.phone, User.google_subject))).all())

    assert client.portal.call(subjects) == {("01012345678", None), (None, "google-user")}


def test_unconfigured_google_login_is_unavailable(client: TestClient) -> None:
    assert client.post("/v1/auth/google/challenge").status_code == 503
    payload = {"challengeId": "0" * 36, "idToken": "token", "role": "CHILD"}
    assert client.post("/v1/auth/google", json=payload).status_code == 503
    _, sms = create_user(client, "01012345678", "CHILD", "사용자")
    assert sms["phone"] == "01012345678"


def test_google_account_can_be_deleted_without_apple_reauthentication(
    client: TestClient, sign_google
) -> None:
    login = client.post("/v1/auth/google", json=google_payload(client, sign_google)).json()
    response = client.delete("/v1/account", headers=auth(login["accessToken"]))
    assert response.status_code == 204, response.text

    async def remaining() -> list[str | None]:
        async with client.app.state.container.database.sessions() as session:
            return list(await session.scalars(select(User.google_subject)))

    assert client.portal.call(remaining) == []


def test_signup_without_role_has_no_family_until_the_first_invite(
    client: TestClient, sign_google
) -> None:
    payload = google_payload(client, sign_google)
    del payload["role"]
    signed_in = client.post("/v1/auth/google", json=payload)
    assert signed_in.status_code == 200, signed_in.text
    user = signed_in.json()["user"]
    assert user["role"] == "CHILD"
    assert user["familyId"] is None
    token = signed_in.json()["accessToken"]
    created = client.post("/v1/families", headers=auth(token))
    assert created.status_code == 201
    invitation = client.post(
        f"/v1/families/{created.json()['familyId']}/invitations",
        headers=auth(token),
        json={"name": "어머니", "relation": "MOTHER"},
    )
    assert invitation.status_code == 201
    again = client.post("/v1/auth/google", json=google_payload(client, sign_google))
    assert again.json()["user"]["familyId"] == created.json()["familyId"]


def test_verified_google_email_is_saved_on_the_account(client: TestClient, sign_google) -> None:
    def signed_in(**claims: object) -> dict:
        challenge = client.post("/v1/auth/google/challenge").json()
        payload = {
            "challengeId": challenge["challengeId"],
            "idToken": sign_google(challenge["nonce"], "google-user", **claims),
        }
        response = client.post("/v1/auth/google", json=payload)
        assert response.status_code == 200, response.text
        return response.json()["user"]

    assert signed_in(email="unverified@example.com", email_verified=False)["email"] is None
    assert signed_in(email="mom@example.com", email_verified=True)["email"] == "mom@example.com"
    assert signed_in()["email"] == "mom@example.com"
    assert signed_in(email="new@example.com", email_verified=True)["email"] == "new@example.com"
