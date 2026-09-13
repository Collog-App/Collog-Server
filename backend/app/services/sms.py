from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings


class SmsDeliveryError(Exception):
    pass


class RegistrationCount(BaseModel):
    registered_success: int = Field(alias="registeredSuccess")
    registered_failed: int = Field(alias="registeredFailed")


class MessageGroup(BaseModel):
    count: RegistrationCount


class SolapiResponse(BaseModel):
    group_info: MessageGroup = Field(alias="groupInfo")


async def send_otp(settings: Settings, phone: str, code: str) -> None:
    if settings.app_env == "test" and settings.mock_external_services:
        return
    if not all((settings.solapi_api_key, settings.solapi_api_secret, settings.solapi_sender)):
        raise SmsDeliveryError("문자 발송 설정을 확인해주세요")
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    salt = secrets.token_hex(16)
    signature = hmac.new(
        settings.solapi_api_secret.encode(),
        (timestamp + salt).encode(),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"HMAC-SHA256 apiKey={settings.solapi_api_key}, date={timestamp}, "
        f"salt={salt}, signature={signature}"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.solapi.com/messages/v4/send-many/detail",
                headers={"Authorization": authorization},
                json={
                    "messages": [{
                        "to": phone,
                        "from": settings.solapi_sender,
                        "type": "SMS",
                        "text": f"[Collog] 인증번호 [{code}]를 입력해주세요.",
                    }],
                },
            )
            response.raise_for_status()
            result = SolapiResponse.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as exc:
        raise SmsDeliveryError("인증 문자를 보내지 못했습니다. 잠시 후 다시 시도해주세요") from exc
    if result.group_info.count.registered_success != 1 or result.group_info.count.registered_failed:
        raise SmsDeliveryError("인증 문자를 보내지 못했습니다. 잠시 후 다시 시도해주세요")
