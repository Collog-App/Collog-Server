from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from app.config import Settings
from app.services.google_auth import OidcIdentityVerifier

KAKAO_KEYS_URL = "https://kauth.kakao.com/.well-known/jwks.json"
KAKAO_ISSUERS = ("https://kauth.kakao.com",)


class KakaoIdentityVerifier(OidcIdentityVerifier):
    """카카오 SDK 로그인(OpenID Connect)이 발급한 ID 토큰을 검증한다.

    오류는 Google 과 같은 GoogleIdentityError(401)·GoogleServiceError(503)를 던진다.
    """

    keys_url = KAKAO_KEYS_URL
    issuers = KAKAO_ISSUERS

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(settings.kakao_app_key, client=client, clock=clock)

    def _email(self, claims: dict) -> str | None:
        # 카카오는 이용자가 이메일 제공에 동의했을 때만 email 을 넣는다
        email = claims.get("email")
        if not isinstance(email, str) or not 0 < len(email) <= 320:
            return None
        return email

    def _name(self, claims: dict) -> str | None:
        nickname = claims.get("nickname")
        if not isinstance(nickname, str) or not nickname.strip():
            return None
        return nickname.strip()[:80]
