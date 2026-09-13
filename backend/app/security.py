from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import get_session
from app.models import RefreshSession, User, UserRole

bearer = HTTPBearer(auto_error=False)


def otp_hash(phone: str, code: str, secret: str) -> str:
    return hashlib.sha256(f"{phone}:{code}:{secret}".encode()).hexdigest()


def issue_token(user: User, settings: Settings, session_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user.id,
            "role": user.role,
            "type": "access",
            "sid": session_id,
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 필요합니다")
    settings: Settings = request.app.state.container.settings
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub", "sid", "type"]},
        )
        if payload["type"] != "access" or not isinstance(payload["sid"], str):
            raise ValueError("not an access token")
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "유효하지 않은 인증 정보입니다") from exc
    auth_session = await session.get(RefreshSession, payload["sid"])
    if (
        auth_session is None
        or auth_session.user_id != payload["sub"]
        or auth_session.revoked_at is not None
        or auth_session.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 만료되었습니다")
    user = await session.get(User, payload["sub"])
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없습니다")
    request.state.auth_session_id = auth_session.id
    return user


CurrentUser = Annotated[User, Depends(current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def require_role(user: User, role: UserRole) -> None:
    if user.role != role.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이 계정으로 수행할 수 없는 작업입니다")
