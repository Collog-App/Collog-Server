from __future__ import annotations

import base64
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.services.google_auth import (
    GOOGLE_KEYS_URL,
    GoogleIdentityError,
    GoogleIdentityVerifier,
    GoogleServiceError,
)

CLIENT_ID = "test-web-client.apps.googleusercontent.com"


def settings() -> Settings:
    return Settings(_env_file=None, google_client_id=CLIENT_ID)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def claims():
    return {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-user-123",
        "nonce": "server-challenge-nonce",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }


def encode(signing_key, claims, kid="google-key"):
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": kid})


def key_document(signing_key):
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    return {"keys": [{**key, "kid": "google-key", "alg": "RS256", "use": "sig"}]}


def keys_client(signing_key) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=key_document(signing_key)))
    )


async def test_verified_subject_and_cached_keys(signing_key, claims):
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == GOOGLE_KEYS_URL
        return httpx.Response(200, json=key_document(signing_key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        token = encode(signing_key, claims)
        for _ in range(2):
            identity = await verifier.verify(token)
            assert identity.subject == claims["sub"]
            assert identity.nonce == claims["nonce"]
        for number in range(10):
            with pytest.raises(GoogleIdentityError):
                await verifier.verify(encode(signing_key, claims, kid=f"unknown-{number}"))
        assert len(requests) == 1


@pytest.mark.parametrize("issuer", ["accounts.google.com", "https://accounts.google.com"])
async def test_accepts_both_google_issuers(signing_key, claims, issuer):
    claims["iss"] = issuer
    async with keys_client(signing_key) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        assert (await verifier.verify(encode(signing_key, claims))).subject == claims["sub"]


@pytest.mark.parametrize("seconds_ahead", [5, 55])
async def test_tolerates_server_clock_behind_google(signing_key, claims, seconds_ahead):
    # 서버 시계가 Google보다 몇 초 느리면 막 발급된 토큰의 iat가 미래로 보인다.
    claims["iat"] = int(time.time()) + seconds_ahead
    async with keys_client(signing_key) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        assert (await verifier.verify(encode(signing_key, claims))).subject == claims["sub"]


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://attacker.example"),
        ("iss", "accounts.google.com.attacker.example"),
        ("iss", "google.com"),
        ("aud", "another-app.apps.googleusercontent.com"),
        ("exp", 1),
        ("iat", 9999999999),
        ("iat", []),
        ("exp", {}),
        ("sub", ""),
        ("sub", 123),
        ("nonce", ""),
        ("nonce", 123),
    ],
)
async def test_rejects_invalid_claims(signing_key, claims, claim, value):
    claims[claim] = value
    async with keys_client(signing_key) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        with pytest.raises(GoogleIdentityError):
            await verifier.verify(encode(signing_key, claims))


@pytest.mark.parametrize("claim", ["sub", "nonce", "iat", "exp", "iss", "aud"])
async def test_requires_claims(signing_key, claims, claim):
    del claims[claim]
    async with keys_client(signing_key) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        with pytest.raises(GoogleIdentityError):
            await verifier.verify(encode(signing_key, claims))


async def test_rejects_wrong_signature(signing_key, claims):
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with keys_client(signing_key) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        with pytest.raises(GoogleIdentityError):
            await verifier.verify(encode(attacker_key, claims))


async def test_unconfigured_client_id_rejects_before_network(signing_key, claims):
    def unexpected_request(_):
        pytest.fail("Unconfigured verifier must not fetch keys")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        verifier = GoogleIdentityVerifier(Settings(_env_file=None), client=client)
        with pytest.raises(GoogleIdentityError):
            await verifier.verify(encode(signing_key, claims))


async def test_deeply_nested_header_is_identity_error():
    def unexpected_request(_):
        pytest.fail("Malformed header must not fetch keys")

    header = base64.urlsafe_b64encode(b"[" * 12000).rstrip(b"=").decode()
    token = f"{header}.e30.sig"
    assert len(token) <= 16384
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        with pytest.raises(GoogleIdentityError):
            await verifier.verify(token)


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
async def test_rejects_algorithm_before_network(claims, algorithm):
    def unexpected_request(_):
        pytest.fail("Invalid algorithm must not fetch keys")

    token = jwt.encode(
        claims,
        "attacker-secret-with-at-least-32-bytes" if algorithm == "HS256" else None,
        algorithm=algorithm,
        headers={"kid": "google-key"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        with pytest.raises(GoogleIdentityError):
            await verifier.verify(token)


async def test_provider_failure_has_backoff_and_recovers(signing_key, claims):
    now = [0.0]
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=key_document(signing_key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client, clock=lambda: now[0])
        token = encode(signing_key, claims)
        for _ in range(2):
            with pytest.raises(GoogleServiceError):
                await verifier.verify(token)
        assert len(requests) == 1
        now[0] = 61
        assert (await verifier.verify(token)).subject == claims["sub"]
        now[0] += 3601
        assert (await verifier.verify(token)).subject == claims["sub"]
        assert len(requests) == 3


@pytest.mark.parametrize("document", [{}, {"keys": []}, {"keys": [None]}, {"keys": "invalid"}])
async def test_malformed_key_response_is_service_error(signing_key, claims, document):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=document))
    ) as client:
        verifier = GoogleIdentityVerifier(settings(), client=client)
        with pytest.raises(GoogleServiceError):
            await verifier.verify(encode(signing_key, claims))
