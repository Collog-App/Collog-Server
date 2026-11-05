from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import jwt

from app.config import Settings
from app.services.notifications import (
    DisabledVoipPushGateway,
    IncomingCallPush,
    MockReportPushGateway,
    MockVoipPushGateway,
    PushNotificationError,
    ReportPushGateway,
    ReportReadyPush,
    UnregisteredPushToken,
    UnregisteredVoipToken,
    VoipPushGateway,
)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class FcmUnregistered(PushNotificationError):
    pass


class FcmClient:
    """Android 기기로 FCM HTTP v1 데이터 메시지를 보낸다.

    서비스 계정 키로 OAuth 접근 토큰을 직접 발급받아 1시간 가까이 재사용한다.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        credentials: dict | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if credentials is None:
            if settings.fcm_service_account_path is None:
                raise PushNotificationError("FCM_SERVICE_ACCOUNT_PATH가 필요합니다")
            try:
                credentials = json.loads(settings.fcm_service_account_path.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                raise PushNotificationError("FCM 서비스 계정 키를 읽을 수 없습니다") from exc
        missing = [
            key for key in ("project_id", "client_email", "private_key")
            if not isinstance(credentials.get(key), str) or not credentials[key]
        ]
        if missing:
            raise PushNotificationError(f"FCM 서비스 계정 키에 값이 없습니다: {', '.join(missing)}")
        self.project_id: str = credentials["project_id"]
        self._email: str = credentials["client_email"]
        self._private_key: str = credentials["private_key"]
        self._key_id: str | None = credentials.get("private_key_id")
        self._token_uri: str = credentials.get("token_uri") or DEFAULT_TOKEN_URI
        if self._token_uri != DEFAULT_TOKEN_URI:
            raise PushNotificationError("FCM 서비스 계정 키의 token_uri가 Google 기본값이 아닙니다")
        self._client = client or httpx.AsyncClient(timeout=10)
        self._owns_client = client is None
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def access_token(self) -> str:
        async with self._lock:
            now = self._clock()
            if self._access_token and now < self._expires_at - 60:
                return self._access_token
            issued = int(now)
            headers = {"kid": self._key_id} if self._key_id else None
            try:
                assertion = jwt.encode(
                    {
                        "iss": self._email,
                        "scope": FCM_SCOPE,
                        "aud": self._token_uri,
                        "iat": issued,
                        "exp": issued + 3600,
                    },
                    self._private_key,
                    algorithm="RS256",
                    headers=headers,
                )
                response = await self._client.post(
                    self._token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
            except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
                raise PushNotificationError("FCM 인증 요청 실패") from exc
            if response.status_code != 200:
                raise PushNotificationError(f"FCM 인증 실패 HTTP {response.status_code}")
            try:
                body = response.json()
                token = body["access_token"]
                expires_in = int(body.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError) as exc:
                raise PushNotificationError("FCM 인증 응답을 이해하지 못했습니다") from exc
            self._access_token = token
            self._expires_at = now + expires_in
            return token

    async def send(
        self, token: str, data: dict[str, str], ttl_seconds: int, collapse_key: str | None = None
    ) -> None:
        if not token:
            raise FcmUnregistered("FCM 토큰이 비어 있습니다")
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        message = {
            "message": {
                "token": token,
                "data": data,
                "android": {"priority": "HIGH", "ttl": f"{max(0, ttl_seconds)}s"},
            }
        }
        if collapse_key:
            message["message"]["android"]["collapse_key"] = collapse_key
        try:
            response = await self._client.post(
                url,
                headers={"authorization": f"Bearer {await self.access_token()}"},
                json=message,
            )
        except httpx.HTTPError as exc:
            raise PushNotificationError("FCM 요청 실패") from exc
        if response.status_code == 200:
            return
        code = self.error_code(response)
        if response.status_code == 401:
            # 캐시한 접근 토큰이 무효가 됐을 수 있다. 다음 발송에서 새로 받는다.
            self._access_token = None
        if self.is_dead_token(response, code):
            raise FcmUnregistered(code or "UNREGISTERED")
        raise PushNotificationError(f"FCM 발송 실패 HTTP {response.status_code}: {code or '-'}")

    @staticmethod
    def is_dead_token(response: httpx.Response, code: str | None) -> bool:
        """기기 행에서 지워도 되는 토큰인지. INVALID_ARGUMENT는 크기·키·TTL 문제일 수도 있어서
        Google이 `message.token` 필드를 지목했을 때만 죽은 토큰으로 본다."""
        if code == "UNREGISTERED":
            return True
        if code != "INVALID_ARGUMENT":
            return False
        try:
            details = response.json().get("error", {}).get("details") or []
        except (ValueError, AttributeError):
            return False
        return any(
            isinstance(detail, dict)
            and any(
                isinstance(violation, dict) and violation.get("field") == "message.token"
                for violation in detail.get("fieldViolations") or []
            )
            for detail in details
        )

    @staticmethod
    def error_code(response: httpx.Response) -> str | None:
        try:
            error = response.json().get("error", {})
        except (ValueError, AttributeError):
            return None
        if not isinstance(error, dict):
            return None
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and isinstance(detail.get("errorCode"), str):
                return detail["errorCode"]
        status = error.get("status")
        return status if isinstance(status, str) else None


def incoming_call_data(push: IncomingCallPush) -> dict[str, str]:
    # FCM data 값은 문자열만 된다. 건강 정보와 LiveKit 자격 증명은 넣지 않는다(iOS와 같음).
    data = {
        "type": "incoming_call",
        "callId": push.call_id,
        "callUUID": push.call_id,
        "callerId": push.caller_id,
        "callerName": push.caller_name,
        "expiresAt": push.expires_at.astimezone(UTC).isoformat(),
    }
    if push.callee_id:
        data["calleeId"] = push.callee_id
    return data


def report_ready_data(push: ReportReadyPush) -> dict[str, str]:
    return {
        "type": "report_ready",
        "callId": push.call_id,
        "title": "Collog",
        "body": "새 통화 기록을 확인해 주세요.",
    }


def seconds_until(moment: datetime) -> int:
    return int((moment.astimezone(UTC) - datetime.now(UTC)).total_seconds())


class FcmVoipPushGateway(VoipPushGateway):
    def __init__(self, client: FcmClient) -> None:
        self.client = client

    async def send_incoming_call(self, token: str, push: IncomingCallPush) -> None:
        try:
            await self.client.send(token, incoming_call_data(push), seconds_until(push.expires_at))
        except FcmUnregistered as exc:
            raise UnregisteredVoipToken(str(exc)) from exc
        return None

    async def close(self) -> None:
        await self.client.close()


class FcmReportPushGateway(ReportPushGateway):
    def __init__(self, client: FcmClient) -> None:
        self.client = client

    async def send_report(self, token: str, push: ReportReadyPush) -> None:
        try:
            await self.client.send(
                token,
                report_ready_data(push),
                seconds_until(push.expires_at),
                collapse_key=push.call_id,
            )
        except FcmUnregistered as exc:
            raise UnregisteredPushToken(str(exc)) from exc
        return None


def create_android_push_gateways(
    settings: Settings,
) -> tuple[VoipPushGateway, ReportPushGateway | None]:
    """Android 통화·리포트 발송기. 둘은 FCM 접근 토큰을 같이 쓴다.

    FCM을 설정하지 않으면 리포트 발송기는 None이다. 그러면 리포트 알림이 Android 기기를
    아예 대상에서 빼서, 실패한 Android 행 때문에 리포트가 계속 재시도되지 않는다.
    """
    if settings.mock_external_services:
        return MockVoipPushGateway(), MockReportPushGateway()
    if settings.fcm_service_account_path is None:
        return DisabledVoipPushGateway(), None
    client = FcmClient(settings)
    return FcmVoipPushGateway(client), FcmReportPushGateway(client)
