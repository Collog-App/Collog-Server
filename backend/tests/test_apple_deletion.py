from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from app.config import Settings
from app.models import AppleLoginChallenge, User
from app.services.apple_auth import AppleIdentity, AppleServiceError
from app.services.apple_oauth import apple_client_secret, revoke_apple_authorization
from tests.conftest import auth, create_user


def test_client_secret_is_short_lived_and_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "apple.p8"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    settings = Settings(apple_team_id="TEAM", apple_key_id="KEY", apple_private_key_path=path)
    token = apple_client_secret(settings)
    claims = jwt.decode(
        token, key.public_key(), algorithms=["ES256"], audience="https://appleid.apple.com"
    )
    assert claims["iss"] == "TEAM"
    assert claims["sub"] == settings.apple_client_id
    assert claims["exp"] - claims["iat"] == 300
    assert jwt.get_unverified_header(token)["kid"] == "KEY"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_subject", [False, True])
async def test_exchange_binds_identity_before_revoke(wrong_subject):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"id_token": "identity", "refresh_token": "refresh"})
        return httpx.Response(200)

    class Verifier:
        async def verify(self, token):
            assert token == "identity"
            return AppleIdentity(subject="other" if wrong_subject else "owner", nonce="nonce")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        kwargs = dict(
            code="code", subject="owner", nonce="nonce", client_secret="secret", client=client
        )
        if wrong_subject:
            with pytest.raises(AppleServiceError):
                await revoke_apple_authorization(Settings(), Verifier(), **kwargs)
            assert len(requests) == 1
        else:
            await revoke_apple_authorization(Settings(), Verifier(), **kwargs)
            assert parse_qs(requests[0].content.decode())["grant_type"] == ["authorization_code"]
            assert parse_qs(requests[1].content.decode())["token"] == ["refresh"]


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "wrong_user",
        "wrong_nonce",
        "revoke_failure",
        "missing_body",
        "expired",
        "consumed",
        "missing_config",
    ],
)
def test_apple_deletion_requires_matching_reauthentication(client, monkeypatch, case):
    token, user = create_user(client, "01092223333", "CHILD", "테스트")
    container = client.app.state.container

    async def seed():
        async with container.database.sessions() as session:
            stored = await session.get(User, user["id"])
            stored.apple_subject = "owner"
            await session.commit()

    client.portal.call(seed)
    challenge = client.post("/v1/auth/apple/challenge").json()

    async def invalidate():
        async with container.database.sessions() as session:
            stored = await session.get(AppleLoginChallenge, challenge["challengeId"])
            if case == "expired":
                stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                stored.consumed_at = datetime.now(UTC)
            await session.commit()

    if case in {"expired", "consumed"}:
        client.portal.call(invalidate)

    async def verify(token):
        return AppleIdentity(
            subject="other" if case == "wrong_user" else "owner",
            nonce="other" if case == "wrong_nonce" else challenge["nonce"],
        )

    revoked = []

    async def revoke(*args, **kwargs):
        revoked.append(kwargs)
        if case == "revoke_failure":
            raise AppleServiceError("unavailable")

    monkeypatch.setattr(container.apple_identity, "verify", verify)

    def secret(settings):
        if case == "missing_config":
            raise AppleServiceError("unconfigured")
        return "secret"

    monkeypatch.setattr("app.account_router.apple_client_secret", secret)
    monkeypatch.setattr("app.account_router.revoke_apple_authorization", revoke)
    body = {
        "challengeId": challenge["challengeId"],
        "identityToken": "identity",
        "authorizationCode": "code",
    }
    response = client.request(
        "DELETE", "/v1/account", headers=auth(token), json=None if case == "missing_body" else body
    )
    expected = {
        "success": 204,
        "wrong_user": 401,
        "wrong_nonce": 401,
        "revoke_failure": 503,
        "missing_body": 400,
        "expired": 401,
        "consumed": 401,
        "missing_config": 503,
    }
    assert response.status_code == expected[case], response.text
    assert bool(revoked) == (case in {"success", "revoke_failure"})

    async def exists():
        async with container.database.sessions() as session:
            return await session.scalar(select(User.id).where(User.id == user["id"]))

    assert bool(client.portal.call(exists)) == (case != "success")
