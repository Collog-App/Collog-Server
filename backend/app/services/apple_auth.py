from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import jwt

from app.config import Settings


class AppleIdentityError(Exception):
    pass


class AppleServiceError(Exception):
    pass


@dataclass(frozen=True)
class AppleIdentity:
    subject: str
    nonce: str


class AppleIdentityVerifier:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audience = settings.apple_client_id
        self._client = client or httpx.AsyncClient(timeout=10)
        self._owns_client = client is None
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = self._clock()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            if now < self._retry_at:
                if now >= self._expires_at:
                    raise AppleServiceError("Apple signing keys are temporarily unavailable")
                raise AppleIdentityError("Unknown Apple signing key")
            self._retry_at = now + 60
            try:
                response = await self._client.get("https://appleid.apple.com/auth/keys")
                response.raise_for_status()
                document = response.json()
                if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                    raise ValueError("Invalid key document")
                keys: dict[str, jwt.PyJWK] = {}
                for item in document["keys"]:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid signing key")
                    if item.get("kty") != "RSA" or item.get("alg") != "RS256":
                        continue
                    key_id = item.get("kid")
                    if not isinstance(key_id, str) or not key_id or item.get("use") != "sig":
                        continue
                    keys[key_id] = jwt.PyJWK.from_dict(item, algorithm="RS256")
                if not keys:
                    raise ValueError("No signing keys")
            except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
                raise AppleServiceError("Could not obtain Apple signing keys") from exc
            self._keys = keys
            self._expires_at = now + 3600
            if kid not in keys:
                raise AppleIdentityError("Unknown Apple signing key")
            return keys[kid]

    async def verify(self, identity_token: str) -> AppleIdentity:
        if not identity_token or len(identity_token) > 16384 or not self._audience:
            raise AppleIdentityError("Invalid Apple identity token")
        try:
            header = jwt.get_unverified_header(identity_token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise AppleIdentityError("Invalid Apple signing algorithm or key")
            key = await self._key(kid)
            claims = jwt.decode(
                identity_token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer="https://appleid.apple.com",
                options={"require": ["exp", "iat", "sub", "nonce", "iss", "aud"]},
            )
            subject = claims["sub"]
            nonce = claims["nonce"]
            if not isinstance(subject, str) or not subject or len(subject) > 255:
                raise AppleIdentityError("Invalid Apple subject")
            if not isinstance(nonce, str) or not nonce or len(nonce) > 255:
                raise AppleIdentityError("Invalid Apple nonce")
            return AppleIdentity(subject=subject, nonce=nonce)
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise AppleIdentityError("Invalid Apple identity token") from exc
