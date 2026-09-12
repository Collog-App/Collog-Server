from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.services.apple_auth import AppleIdentityError, AppleIdentityVerifier, AppleServiceError


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def claims():
    return {
        "iss": "https://appleid.apple.com",
        "aud": "com.dohyeoplim.collog-ios",
        "sub": "apple-user-123",
        "nonce": "server-challenge-hash",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }


def encode(signing_key, claims, kid="apple-key"):
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": kid})


def key_document(signing_key):
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    return {"keys": [{**key, "kid": "apple-key", "alg": "RS256", "use": "sig"}]}


async def test_verified_subject_and_cached_keys(signing_key, claims):
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://appleid.apple.com/auth/keys"
        return httpx.Response(200, json=key_document(signing_key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        verifier = AppleIdentityVerifier(Settings(_env_file=None), client=client)
        token = encode(signing_key, claims)
        for _ in range(2):
            identity = await verifier.verify(token)
            assert identity.subject == claims["sub"]
            assert identity.nonce == claims["nonce"]
        for number in range(10):
            with pytest.raises(AppleIdentityError):
                await verifier.verify(encode(signing_key, claims, kid=f"unknown-{number}"))
        assert len(requests) == 1


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://attacker.example"),
        ("aud", "another-app"),
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
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=key_document(signing_key)))
    ) as client:
        verifier = AppleIdentityVerifier(Settings(_env_file=None), client=client)
        with pytest.raises(AppleIdentityError):
            await verifier.verify(encode(signing_key, claims))


@pytest.mark.parametrize("claim", ["sub", "nonce", "iat", "exp", "iss", "aud"])
async def test_requires_claims(signing_key, claims, claim):
    del claims[claim]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=key_document(signing_key)))
    ) as client:
        verifier = AppleIdentityVerifier(Settings(_env_file=None), client=client)
        with pytest.raises(AppleIdentityError):
            await verifier.verify(encode(signing_key, claims))


async def test_rejects_wrong_signature(signing_key, claims):
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=key_document(signing_key)))
    ) as client:
        verifier = AppleIdentityVerifier(Settings(_env_file=None), client=client)
        with pytest.raises(AppleIdentityError):
            await verifier.verify(encode(attacker_key, claims))


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
async def test_rejects_algorithm_before_network(claims, algorithm):
    def unexpected_request(_):
        pytest.fail("Invalid algorithm must not fetch keys")

    token = jwt.encode(
        claims,
        "attacker-secret-with-at-least-32-bytes" if algorithm == "HS256" else None,
        algorithm=algorithm,
        headers={"kid": "apple-key"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        verifier = AppleIdentityVerifier(Settings(_env_file=None), client=client)
        with pytest.raises(AppleIdentityError):
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
        verifier = AppleIdentityVerifier(
            Settings(_env_file=None), client=client, clock=lambda: now[0]
        )
        token = encode(signing_key, claims)
        for _ in range(2):
            with pytest.raises(AppleServiceError):
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
        verifier = AppleIdentityVerifier(Settings(_env_file=None), client=client)
        with pytest.raises(AppleServiceError):
            await verifier.verify(encode(signing_key, claims))
