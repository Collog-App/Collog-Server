from __future__ import annotations

import time

import httpx
import jwt

from app.config import Settings
from app.services.apple_auth import AppleIdentityVerifier, AppleServiceError


def apple_client_secret(settings: Settings) -> str:
    if not all((settings.apple_team_id, settings.apple_key_id, settings.apple_private_key_path)):
        raise AppleServiceError("Apple account deletion is not configured")
    try:
        key = settings.apple_private_key_path.read_text()
        now = int(time.time())
        return jwt.encode(
            {
                "iss": settings.apple_team_id,
                "iat": now,
                "exp": now + 300,
                "aud": "https://appleid.apple.com",
                "sub": settings.apple_client_id,
            },
            key,
            algorithm="ES256",
            headers={"kid": settings.apple_key_id},
        )
    except (OSError, ValueError, jwt.PyJWTError) as exc:
        raise AppleServiceError("Apple account deletion credentials are unavailable") from exc


async def revoke_apple_authorization(
    settings: Settings,
    verifier: AppleIdentityVerifier,
    *,
    code: str,
    subject: str,
    nonce: str,
    client_secret: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        response = await client.post(
            "https://appleid.apple.com/auth/token",
            data={
                "client_id": settings.apple_client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid Apple token response")
        identity_token = payload.get("id_token")
        refresh_token = payload.get("refresh_token")
        if (
            not isinstance(identity_token, str)
            or not isinstance(refresh_token, str)
            or not refresh_token
        ):
            raise ValueError("Missing Apple tokens")
        identity = await verifier.verify(identity_token)
        if identity.subject != subject or identity.nonce != nonce:
            raise ValueError("Apple authorization does not match this account")
        revoked = await client.post(
            "https://appleid.apple.com/auth/revoke",
            data={
                "client_id": settings.apple_client_id,
                "client_secret": client_secret,
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
        )
        revoked.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppleServiceError("Apple authorization revocation failed") from exc
    finally:
        if owns_client:
            await client.aclose()
