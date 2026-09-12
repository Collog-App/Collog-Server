from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models import (
    AppleLoginChallenge,
    Device,
    Family,
    OtpChallenge,
    RefreshSession,
    User,
    UserRole,
)
from app.schemas import (
    AppleChallengeResponse,
    AppleLoginRequest,
    OtpRequest,
    OtpVerify,
    RefreshRequest,
    TokenResponse,
    UserView,
)
from app.security import SessionDep, issue_token, otp_hash
from app.services.apple_auth import AppleIdentityError, AppleServiceError
from app.services.domain import family_of
from app.services.sms import SmsDeliveryError, send_otp

router = APIRouter(prefix="/auth", tags=["Auth"])


def refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def token_response(
    session: SessionDep,
    user: User,
    settings: Settings,
    auth_session: RefreshSession,
    refresh_token: str,
) -> TokenResponse:
    family = await family_of(session, user)
    return TokenResponse(
        access_token=issue_token(user, settings, auth_session.id),
        refresh_token=refresh_token,
        user=UserView(
            id=user.id,
            role=user.role,
            name=user.name,
            phone=user.phone,
            apple_user_id=user.apple_subject,
            family_id=family.id if family else None,
        ),
    )


async def create_session(session: SessionDep, user: User, settings: Settings) -> TokenResponse:
    if user.role == UserRole.CHILD.value and await family_of(session, user) is None:
        session.add(Family(created_by=user.id))
        await session.flush()
    refresh_token = secrets.token_urlsafe(48)
    auth_session = RefreshSession(
        user_id=user.id,
        token_hash=refresh_hash(refresh_token),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_ttl_days),
    )
    session.add(auth_session)
    await session.flush()
    result = await token_response(session, user, settings, auth_session, refresh_token)
    await session.commit()
    return result


def require_apple_login(settings: Settings) -> None:
    if not settings.apple_login_enabled or not settings.apple_client_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Apple 로그인을 사용할 수 없습니다"
        )


@router.post("/apple/challenge", response_model=AppleChallengeResponse)
async def apple_challenge(request: Request, session: SessionDep) -> AppleChallengeResponse:
    settings: Settings = request.app.state.container.settings
    require_apple_login(settings)
    now = datetime.now(UTC)
    await session.execute(delete(AppleLoginChallenge).where(AppleLoginChallenge.expires_at < now))
    nonce = secrets.token_urlsafe(32)
    challenge = AppleLoginChallenge(
        nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
        expires_at=now + timedelta(seconds=settings.apple_challenge_ttl_seconds),
    )
    session.add(challenge)
    await session.commit()
    return AppleChallengeResponse(
        challenge_id=challenge.id, nonce=nonce, expires_in=settings.apple_challenge_ttl_seconds
    )


@router.post("/apple", response_model=TokenResponse)
async def apple_login(
    payload: AppleLoginRequest, request: Request, session: SessionDep
) -> TokenResponse:
    settings: Settings = request.app.state.container.settings
    require_apple_login(settings)
    challenge = await session.get(AppleLoginChallenge, payload.challenge_id)
    if challenge is None or challenge.consumed_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Apple 로그인을 다시 시작해주세요")
    if challenge.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Apple 로그인 요청이 만료되었습니다")
    try:
        identity = await request.app.state.container.apple_identity.verify(payload.identity_token)
    except AppleIdentityError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Apple 로그인 정보를 확인할 수 없습니다"
        ) from exc
    except AppleServiceError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Apple 로그인 확인을 잠시 후 다시 시도해주세요"
        ) from exc
    nonce_hash = hashlib.sha256(identity.nonce.encode()).hexdigest()
    if not hmac.compare_digest(challenge.nonce_hash, nonce_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Apple 로그인 요청이 일치하지 않습니다")
    consumed = await session.scalar(
        update(AppleLoginChallenge)
        .where(
            AppleLoginChallenge.id == payload.challenge_id,
            AppleLoginChallenge.consumed_at.is_(None),
            AppleLoginChallenge.expires_at > datetime.now(UTC),
        )
        .values(consumed_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
        .returning(AppleLoginChallenge.id)
    )
    if consumed is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "이미 사용한 Apple 로그인 요청입니다")
    user = await session.scalar(select(User).where(User.apple_subject == identity.subject))
    if user is None:
        try:
            async with session.begin_nested():
                user = User(
                    apple_subject=identity.subject,
                    phone=None,
                    role=payload.role,
                    name=(payload.name or "").strip() or "사용자",
                )
                session.add(user)
                await session.flush()
        except IntegrityError:
            user = await session.scalar(select(User).where(User.apple_subject == identity.subject))
            if user is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "Apple 로그인을 다시 시도해주세요"
                ) from None
    return await create_session(session, user, settings)


@router.post("/otp/request", status_code=202)
async def request_otp(
    payload: OtpRequest, request: Request, session: SessionDep
) -> dict[str, int | str]:
    settings: Settings = request.app.state.container.settings
    now = datetime.now(UTC)
    recent_count = await session.scalar(
        select(func.count(OtpChallenge.id)).where(
            OtpChallenge.phone == payload.phone,
            OtpChallenge.created_at >= now - timedelta(minutes=10),
        )
    )
    if (recent_count or 0) >= 5:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "잠시 후 다시 요청해주세요")
    test_mode = settings.app_env == "test" and settings.mock_external_services
    code = settings.dev_otp_code if test_mode else f"{secrets.randbelow(1_000_000):06d}"
    challenge = OtpChallenge(
        phone=payload.phone,
        code_hash=otp_hash(payload.phone, code, settings.jwt_secret),
        requested_role=str(payload.role),
        requested_name=payload.name,
        expires_at=now,
    )
    session.add(challenge)
    await session.commit()
    try:
        await send_otp(settings, payload.phone, code)
    except SmsDeliveryError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    challenge.expires_at = datetime.now(UTC) + timedelta(seconds=settings.otp_ttl_seconds)
    await session.commit()
    result: dict[str, int | str] = {"expiresIn": settings.otp_ttl_seconds}
    if test_mode:
        result["devCode"] = code
    return result


@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(payload: OtpVerify, request: Request, session: SessionDep) -> TokenResponse:
    settings: Settings = request.app.state.container.settings
    latest_id = await session.scalar(
        select(OtpChallenge.id)
        .where(OtpChallenge.phone == payload.phone)
        .order_by(OtpChallenge.created_at.desc())
        .limit(1)
    )
    challenge = await session.scalar(
        update(OtpChallenge)
        .where(
            OtpChallenge.id == latest_id,
            OtpChallenge.verified_at.is_(None),
            OtpChallenge.expires_at > datetime.now(UTC),
            OtpChallenge.attempts < settings.otp_max_attempts,
        )
        .values(attempts=OtpChallenge.attempts + 1)
        .returning(OtpChallenge)
    )
    if challenge is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증번호를 다시 요청해주세요")
    expected = otp_hash(payload.phone, payload.code, settings.jwt_secret)
    if not hmac.compare_digest(challenge.code_hash, expected):
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증번호가 올바르지 않습니다")
    challenge.verified_at = datetime.now(UTC)
    user = await session.scalar(select(User).where(User.phone == payload.phone))
    if user is None:
        user = User(
            phone=payload.phone, role=challenge.requested_role, name=challenge.requested_name
        )
        session.add(user)
        await session.flush()
    return await create_session(session, user, settings)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, request: Request, session: SessionDep) -> TokenResponse:
    settings: Settings = request.app.state.container.settings
    refresh_token = secrets.token_urlsafe(48)
    auth_session = await session.scalar(
        update(RefreshSession)
        .where(
            RefreshSession.token_hash == refresh_hash(payload.refresh_token),
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > datetime.now(UTC),
        )
        .values(
            token_hash=refresh_hash(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_ttl_days),
        )
        .returning(RefreshSession)
    )
    if auth_session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "다시 로그인해주세요")
    user = await session.get(User, auth_session.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "다시 로그인해주세요")
    result = await token_response(session, user, settings, auth_session, refresh_token)
    await session.commit()
    return result


@router.post("/logout", status_code=204)
async def logout(payload: RefreshRequest, session: SessionDep) -> Response:
    auth_session_id = await session.scalar(
        update(RefreshSession)
        .where(RefreshSession.token_hash == refresh_hash(payload.refresh_token))
        .values(revoked_at=datetime.now(UTC))
        .returning(RefreshSession.id)
    )
    if auth_session_id is not None:
        await session.execute(
            update(Device)
            .where(Device.auth_session_id == auth_session_id)
            .values(voip_token=None, push_token=None)
        )
    await session.commit()
    return Response(status_code=204)
