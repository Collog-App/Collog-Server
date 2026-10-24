from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import jwt

from app.config import Settings

GOOGLE_KEYS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleIdentityError(Exception):
    pass


class GoogleServiceError(Exception):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    nonce: str


class GoogleIdentityVerifier:
    """Android Credential Manager가 발급받은 Google ID 토큰을 검증한다.

    AppleIdentityVerifier와 같은 키 캐시·실패 backoff 규칙을 따른다.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audience = settings.google_client_id
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
                    raise GoogleServiceError("Google signing keys are temporarily unavailable")
                raise GoogleIdentityError("Unknown Google signing key")
            self._retry_at = now + 60
            try:
                response = await self._client.get(GOOGLE_KEYS_URL)
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
                raise GoogleServiceError("Could not obtain Google signing keys") from exc
            self._keys = keys
            self._expires_at = now + 3600
            if kid not in keys:
                raise GoogleIdentityError("Unknown Google signing key")
            return keys[kid]

    async def verify(self, id_token: str) -> GoogleIdentity:
        if not id_token or len(id_token) > 16384 or not self._audience:
            raise GoogleIdentityError("Invalid Google ID token")
        try:
            header = jwt.get_unverified_header(id_token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise GoogleIdentityError("Invalid Google signing algorithm or key")
            key = await self._key(kid)
            claims = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=GOOGLE_ISSUERS,
                options={"require": ["exp", "iat", "sub", "nonce", "iss", "aud"]},
            )
            subject = claims["sub"]
            nonce = claims["nonce"]
            if not isinstance(subject, str) or not subject or len(subject) > 255:
                raise GoogleIdentityError("Invalid Google subject")
            if not isinstance(nonce, str) or not nonce or len(nonce) > 255:
                raise GoogleIdentityError("Invalid Google nonce")
            return GoogleIdentity(subject=subject, nonce=nonce)
        # 깊게 중첩된 헤더 JSON은 PyJWT 안에서 RecursionError가 된다(PYSEC-2026-4142).
        except (jwt.PyJWTError, TypeError, ValueError, RecursionError) as exc:
            raise GoogleIdentityError("Invalid Google ID token") from exc
