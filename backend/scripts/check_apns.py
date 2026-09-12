from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.services.notifications import ApnsVoipPushGateway, IncomingCallPush, PushNotificationError

# A reserved token checks APNs connectivity without delivering a notification.
# Only a registered device can verify delivery and the complete topic configuration.
PROBE_TOKEN = "0" * 64

CREDENTIALS_OK_REASONS = frozenset({"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"})

REASON_HINTS = {
    "InvalidProviderToken": (
        "APNS_KEY_ID·APNS_TEAM_ID·.p8 조합이 맞지 않거나, 해당 key에 APNs 서비스가 없다. "
        "Apple Developer > Keys에서 key의 Apple Push Notifications service 활성화를 확인한다."
    ),
    "ExpiredProviderToken": "서버 시계가 어긋났다. 시스템 시간을 NTP로 동기화한 뒤 다시 실행한다.",
    "TooManyProviderTokenUpdates": "provider token을 너무 자주 재발급했다. 몇 분 뒤 다시 실행한다.",
    "MissingTopic": "APNS_BUNDLE_ID가 비어 있다.",
    "BadTopic": (
        "apns-topic이 잘못됐다. VoIP topic은 '<Bundle ID>.voip'이며 App ID에 "
        "Push Notifications capability가 켜져 있어야 한다."
    ),
    "TopicDisallowed": (
        "이 key로는 해당 topic에 보낼 수 없다. Bundle ID가 같은 Apple Developer 팀 소속인지 "
        "확인한다."
    ),
    "DeviceTokenNotForTopic": (
        "토큰이 다른 앱의 것이다. PushKit VoIP 토큰과 Bundle ID가 같은 앱인지 확인한다."
    ),
    "BadDeviceToken": (
        "토큰 형식이 틀렸거나 sandbox/production 환경이 토큰과 맞지 않는다. "
        "Xcode 직접 설치 빌드는 sandbox, TestFlight/App Store 빌드는 production이다."
    ),
    "Unregistered": "앱이 삭제됐거나 토큰이 만료됐다. 기기에서 앱을 다시 실행해 토큰을 재등록한다.",
    "BadEnvironmentKeyInToken": (
        "이 `.p8` key는 해당 환경용으로 발급되지 않았다. Apple Developer > Keys에서 key의 "
        "Environment 설정을 확인한다. 이 값은 저장 후 변경할 수 없으므로 다른 환경이 필요하면 "
        "key를 새로 발급해야 한다."
    ),
    "PayloadTooLarge": "VoIP payload는 5KB를 넘을 수 없다.",
    "Forbidden": "요청이 거부됐다. key 권한과 Bundle ID 소유 팀을 확인한다.",
}


def build_gateway(settings: Settings) -> ApnsVoipPushGateway:
    # The API factory honors MOCK_EXTERNAL_SERVICES and APNS_VOIP_ENABLED. This check must
    # always talk to Apple, so it constructs the real gateway directly.
    return ApnsVoipPushGateway(settings)


async def post_once(
    gateway: ApnsVoipPushGateway, device_token: str, push: IncomingCallPush
) -> httpx.Response:
    normalized = ApnsVoipPushGateway.normalize_device_token(device_token)
    return await gateway.client.post(
        gateway.device_url(normalized),
        headers=gateway.request_headers(),
        json=push.payload(),
    )


def read_reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("reason", ""))
    except ValueError:
        return ""


def report(response: httpx.Response, *, probe: bool) -> bool:
    reason = read_reason(response)
    apns_id = response.headers.get("apns-id", "-")
    print(f"HTTP {response.status_code} {reason or '-'}  apns-id={apns_id}")
    if probe:
        if reason in CREDENTIALS_OK_REASONS:
            print("Apple 접속 확인. 실제 기기 수신은 별도로 확인해야 합니다.")
            return True
    elif response.status_code == 200:
        print("결과: 발송 성공. 기기에서 PushKit delegate와 CallKit 화면을 확인한다.")
        return True
    hint = REASON_HINTS.get(reason)
    print(f"결과: 실패. {hint}" if hint else "결과: 실패. Apple의 reason 값을 문서에서 확인한다.")
    return False


async def run(device_token: str | None, environment: str | None) -> int:
    settings = Settings()
    if environment:
        settings.apns_environment = environment
    try:
        gateway = build_gateway(settings)
    except PushNotificationError as exc:
        print(f"설정 오류: {exc}")
        return 2

    probe = device_token is None
    push = IncomingCallPush(
        call_id=str(uuid.uuid4()),
        caller_id="apns-check",
        caller_name="콜록 점검",
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.incoming_call_ttl_seconds),
    )
    print(f"environment={settings.apns_environment} endpoint={gateway.endpoint}")
    print(f"team={settings.apns_team_id} key={settings.apns_key_id}")
    print(f"topic={settings.apns_bundle_id}.voip")
    print("mode=자격증명 점검(더미 토큰)" if probe else "mode=실기기 발송")

    try:
        response = await post_once(gateway, device_token or PROBE_TOKEN, push)
    except PushNotificationError as exc:
        print(f"토큰 오류: {exc}")
        return 2
    except httpx.HTTPError as exc:
        print(f"APNs 연결 실패: {exc}")
        return 2
    finally:
        await gateway.close()

    return 0 if report(response, probe=probe) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="APNs VoIP 자격증명 점검 및 실기기 push 발송 (.env의 APNS_* 값을 사용)"
    )
    parser.add_argument(
        "--device-token",
        help="PushKit VoIP 토큰(hex). 생략하면 더미 토큰으로 자격증명만 점검한다.",
    )
    parser.add_argument(
        "--environment",
        choices=["sandbox", "production"],
        help="APNS_ENVIRONMENT를 이번 실행에만 덮어쓴다.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.device_token, args.environment)))


if __name__ == "__main__":
    main()
