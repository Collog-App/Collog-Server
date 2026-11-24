from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.schemas import AnonymousEventBatch, AppEventBatch, EventsAccepted
from app.security import CurrentUser, SessionDep
from app.services.events import store_events

router = APIRouter(tags=["Events"])

# 익명 기록 20개면 넉넉히 8KB 안이다. 인증 없는 주소라 본문을 다 읽기 전에 크기부터 막는다.
ANONYMOUS_BODY_LIMIT = 16 * 1024


@router.post("/events", response_model=EventsAccepted)
async def record_events(
    payload: AppEventBatch, user: CurrentUser, session: SessionDep
) -> EventsAccepted:
    """앱 이용 기록을 받는다. 규칙에 맞지 않는 줄은 버리고 받은 수만 돌려준다."""
    return EventsAccepted(accepted=await store_events(session, payload.events, user_id=user.id))


async def limited_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "요청 본문이 너무 큽니다")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, "요청 본문이 너무 큽니다"
            )
    return bytes(body)


@router.post(
    "/events/anonymous",
    response_model=EventsAccepted,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["events"],
                        "properties": {
                            "events": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"$ref": "#/components/schemas/AppEventIn"},
                            }
                        },
                    }
                }
            },
        }
    },
)
async def record_anonymous_events(request: Request, session: SessionDep) -> EventsAccepted:
    """로그인 전 가입 단계 기록(`onboarding_step`)만 받는다. 사용자·대상·통화 ID 는 무시한다."""
    try:
        payload = AnonymousEventBatch.model_validate_json(
            await limited_body(request, ANONYMOUS_BODY_LIMIT)
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from None
    return EventsAccepted(accepted=await store_events(session, payload.events, user_id=None))
