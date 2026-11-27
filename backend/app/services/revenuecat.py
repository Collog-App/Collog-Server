"""RevenueCat REST v1(서버 비밀 키)과 구독 정보 해석.

`GET /v1/subscribers/{app_user_id}` 는 없는 ID 로 부르면 고객을 새로 만든다. 그래서 우리
`users.id` 로만 부른다. 응답·웹훅의 "가끔 있는" 칸은 없을 수 있어 하나씩 방어적으로 읽는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.services.http import request_with_retry

API_BASE_URL = "https://api.revenuecat.com"
# 만료가 없는 권한(평생·프로모션 평생)은 이 시각까지 유효한 것으로 저장한다.
LIFETIME = datetime(9999, 12, 31, tzinfo=UTC)


class RevenueCatError(RuntimeError):
    pass


@dataclass(frozen=True)
class EntitlementState:
    active_until: datetime | None = None
    store: str | None = None
    product_id: str | None = None
    environment: str | None = None
    will_renew: bool | None = None


NO_ENTITLEMENT = EntitlementState()


class RevenueCatClient:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._transport = transport
        # 사용자별 마지막 새로 읽기 요청 시각(time.monotonic). 서버 한 대라 프로세스 메모리면 된다.
        self.refreshed_at: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.settings.revenuecat_secret_key.strip())

    async def _request(self, method: str, app_user_id: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
            return await request_with_retry(
                client,
                method,
                f"{API_BASE_URL}/v1/subscribers/{quote(app_user_id, safe='')}",
                headers={
                    "Authorization": f"Bearer {self.settings.revenuecat_secret_key.strip()}"
                },
            )

    async def subscriber(self, app_user_id: str) -> dict[str, Any]:
        """`subscriber` 객체를 돌려준다. 우리 users.id 만 넘긴다."""
        try:
            payload = (await self._request("GET", app_user_id)).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RevenueCatError(f"RevenueCat 구독 조회 실패: {exc}") from exc
        subscriber = payload.get("subscriber") if isinstance(payload, dict) else None
        if not isinstance(subscriber, dict):
            raise RevenueCatError("RevenueCat 응답에 subscriber 가 없습니다")
        return subscriber


    async def delete_subscriber(self, app_user_id: str) -> None:
        """고객을 지운다(RevenueCat 이 비동기로 처리). 없는 고객(404)은 이미 지워진 것으로 본다."""
        try:
            await self._request("DELETE", app_user_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise RevenueCatError(f"RevenueCat 고객 삭제 실패: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RevenueCatError(f"RevenueCat 고객 삭제 실패: {exc}") from exc


class MockRevenueCatClient(RevenueCatClient):
    """시험용. `subscribers` 에 넣어 둔 값을 돌려주고 부른 ID 를 `requested`·`deleted` 에 남긴다."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.subscribers: dict[str, dict[str, Any]] = {}
        self.requested: list[str] = []
        self.deleted: list[str] = []

    async def subscriber(self, app_user_id: str) -> dict[str, Any]:
        self.requested.append(app_user_id)
        return self.subscribers.get(app_user_id, {"entitlements": {}, "subscriptions": {}})

    async def delete_subscriber(self, app_user_id: str) -> None:
        self.deleted.append(app_user_id)


def create_revenuecat_client(settings: Settings) -> RevenueCatClient:
    if settings.mock_external_services:
        return MockRevenueCatClient(settings)
    return RevenueCatClient(settings)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def text(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _purchase(subscriber: dict[str, Any], product_id: str | None) -> dict[str, Any] | None:
    """권한을 준 구매. 구독은 상품 ID 로 묶여 있고, Play 는 키에 기본 요금제가 빠져 있을 수 있다."""
    if not product_id:
        return None
    subscriptions = subscriber.get("subscriptions")
    if isinstance(subscriptions, dict):
        for key in (product_id, product_id.split(":", 1)[0]):
            if isinstance(subscriptions.get(key), dict):
                return subscriptions[key]
    purchases = (subscriber.get("non_subscriptions") or {}).get(product_id)
    if isinstance(purchases, list) and purchases and isinstance(purchases[-1], dict):
        return purchases[-1]
    return None


def entitlement_from_subscriber(
    subscriber: dict[str, Any], entitlement_id: str, *, allow_sandbox: bool
) -> EntitlementState:
    entitlements = subscriber.get("entitlements")
    entitlement = entitlements.get(entitlement_id) if isinstance(entitlements, dict) else None
    if not isinstance(entitlement, dict):
        return NO_ENTITLEMENT
    product_id = text(entitlement.get("product_identifier"), 120)
    purchase = _purchase(subscriber, product_id)
    sandbox = bool(purchase and purchase.get("is_sandbox") is True)
    if sandbox and not allow_sandbox:
        return NO_ENTITLEMENT
    if "expires_date" in entitlement and entitlement["expires_date"] is None:
        active_until: datetime | None = LIFETIME
        will_renew = None
    else:
        ends = [
            moment
            for moment in (
                parse_time(entitlement.get("expires_date")),
                parse_time(entitlement.get("grace_period_expires_date")),
            )
            if moment is not None
        ]
        active_until = max(ends) if ends else None
        # 구독이고 자동 갱신을 끈 시각(unsubscribe_detected_at)·환불(refunded_at)이 없으면 갱신된다.
        will_renew = (
            not purchase.get("unsubscribe_detected_at") and not purchase.get("refunded_at")
            if purchase is not None and "expires_date" in purchase
            else None
        )
    store = text(purchase.get("store"), 32) if purchase else None
    return EntitlementState(
        active_until=active_until,
        store=store.upper() if store else None,
        product_id=product_id,
        environment=("SANDBOX" if sandbox else "PRODUCTION") if purchase else None,
        will_renew=will_renew,
    )


def from_ms(value: object) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def entitlement_from_event(event: dict[str, Any], entitlement_id: str) -> EntitlementState | None:
    """비밀 키가 없을 때 웹훅 본문만으로 읽는다. 그 권한 이야기가 아니면 None.

    결제 유예(grace_period_expiration_at_ms)가 만료보다 늦으면 유예 끝까지 유효로 본다.
    """
    entitlement_ids = event.get("entitlement_ids")
    if not isinstance(entitlement_ids, list) or entitlement_id not in entitlement_ids:
        return None
    if "expiration_at_ms" not in event:
        return None
    event_type = event.get("type")
    if event["expiration_at_ms"] is None:
        active_until = LIFETIME
    else:
        expiration = from_ms(event["expiration_at_ms"])
        if expiration is None:
            return None
        grace = from_ms(event.get("grace_period_expiration_at_ms"))
        active_until = (
            max(expiration, grace) if grace and event_type != "EXPIRATION" else expiration
        )
    return EntitlementState(
        active_until=active_until,
        store=text(event.get("store"), 32),
        product_id=text(event.get("product_id"), 120),
        environment=text(event.get("environment"), 16),
        will_renew=event_type not in {"CANCELLATION", "EXPIRATION", "NON_RENEWING_PURCHASE"},
    )


def related_user_ids(event: dict[str, Any]) -> set[str]:
    """이벤트에 나온 모든 고객 ID. TRANSFER 는 옮겨 간 쪽(transferred_to)에도 보낸다."""
    ids: set[str] = set()
    for key in ("app_user_id", "original_app_user_id"):
        if isinstance(event.get(key), str):
            ids.add(event[key])
    for key in ("aliases", "transferred_from", "transferred_to"):
        values = event.get(key)
        if isinstance(values, list):
            ids.update(value for value in values if isinstance(value, str))
    return {value for value in ids if value and len(value) <= 36}
