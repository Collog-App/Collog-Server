from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import and_, delete, or_, select, update

from app.models import (
    AcousticAnalysisRun,
    AcousticFeature,
    AppleLoginChallenge,
    AssetKind,
    AudioAsset,
    Baseline,
    CallRecord,
    CallState,
    ChangeSignal,
    ConsentRecord,
    Device,
    ExtractionEvidence,
    Family,
    FamilyMember,
    HealthExtraction,
    Invitation,
    OtpChallenge,
    ParentProfile,
    QuestionTtsGrant,
    RefreshSession,
    RepeatEvent,
    Report,
    Transcript,
    User,
    UserRole,
)
from app.schemas import ApiModel, UserView
from app.security import CurrentUser, SessionDep
from app.services.apple_auth import AppleIdentityError, AppleServiceError
from app.services.apple_oauth import apple_client_secret, revoke_apple_authorization
from app.services.domain import family_of
from app.services.storage import StorageError

router = APIRouter(prefix="/account", tags=["Account"])


class RoleUpdate(ApiModel):
    role: UserRole


class AppleDeletionRequest(ApiModel):
    challenge_id: str = Field(min_length=1, max_length=255)
    identity_token: str = Field(min_length=1, max_length=16384)
    authorization_code: str = Field(min_length=1, max_length=4096)


async def lock_account(session: SessionDep, user: User, *, deleting: bool = False) -> None:
    locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None:
        raise HTTPException(401, "다시 로그인해주세요")
    await session.refresh(user)
    busy_states = [CallState.CREATED.value, CallState.RINGING.value, CallState.ACTIVE.value]
    if deleting:
        busy_states.append(CallState.PROCESSING.value)
    busy_condition = CallRecord.state.in_(busy_states)
    if deleting:
        busy_condition = or_(busy_condition, and_(
            CallRecord.state == CallState.ENDED.value,
            or_(CallRecord.recording_enabled.is_(True), CallRecord.ended_at.is_(None)),
        ))
    busy = await session.scalar(
        select(CallRecord.id)
        .where(
            or_(CallRecord.parent_id == user.id, CallRecord.child_id == user.id),
            busy_condition,
        )
        .limit(1)
    )
    if busy:
        raise HTTPException(409, "통화와 음성 분석이 끝난 후 다시 시도해주세요")


@router.patch("/role", response_model=UserView)
async def update_role(
    payload: RoleUpdate, request: Request, user: CurrentUser, session: SessionDep
) -> UserView:
    async with request.app.state.container.calls.reserve_participants([user.id]):
        await lock_account(session, user)
        user.role = payload.role
        family = await family_of(session, user)
        if family is None and user.role == UserRole.CHILD.value:
            family = Family(created_by=user.id)
            session.add(family)
            await session.flush()
        result = UserView(
            id=user.id,
            role=user.role,
            name=user.name,
            phone=user.phone,
            apple_user_id=user.apple_subject,
            family_id=family.id if family else None,
        )
        await session.commit()
        return result


@router.delete("", status_code=204)
async def delete_account(
    request: Request, user: CurrentUser, session: SessionDep,
    payload: AppleDeletionRequest | None = None,
) -> Response:
    async with request.app.state.container.calls.reserve_participants([user.id]):
        await lock_account(session, user, deleting=True)
        container = request.app.state.container
        identity = None
        client_secret = None
        if user.apple_subject:
            if payload is None:
                raise HTTPException(400, "계정 삭제를 위해 Apple 인증을 다시 진행해주세요")
            try:
                client_secret = apple_client_secret(container.settings)
                identity = await container.apple_identity.verify(payload.identity_token)
            except AppleIdentityError as exc:
                raise HTTPException(401, "Apple 인증을 다시 진행해주세요") from exc
            except AppleServiceError as exc:
                raise HTTPException(
                    503, "Apple 계정 삭제를 준비 중이에요. 잠시 후 다시 시도해주세요"
                ) from exc
            challenge = await session.get(AppleLoginChallenge, payload.challenge_id)
            if (
                identity.subject != user.apple_subject or challenge is None
                or challenge.consumed_at is not None
                or challenge.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
                or not hmac.compare_digest(
                    challenge.nonce_hash, hashlib.sha256(identity.nonce.encode()).hexdigest()
                )
            ):
                raise HTTPException(401, "현재 계정으로 Apple 인증을 다시 진행해주세요")
            consumed = await session.scalar(
                update(AppleLoginChallenge).where(
                    AppleLoginChallenge.id == payload.challenge_id,
                    AppleLoginChallenge.consumed_at.is_(None),
                    AppleLoginChallenge.expires_at > datetime.now(UTC),
                ).values(consumed_at=datetime.now(UTC))
                .execution_options(synchronize_session=False).returning(AppleLoginChallenge.id)
            )
            if consumed is None:
                raise HTTPException(401, "Apple 인증을 다시 진행해주세요")
        calls = list(await session.scalars(
            select(CallRecord).where(
                or_(CallRecord.parent_id == user.id, CallRecord.child_id == user.id)
            ).with_for_update()
        ))
        call_ids = [call.id for call in calls]
        parent_ids = {user.id, *(call.parent_id for call in calls)}
        assets = list(await session.scalars(
            select(AudioAsset).where(AudioAsset.call_id.in_(call_ids))
        ))
        upload_ttl = request.app.state.container.settings.upload_url_ttl_seconds
        now = datetime.now(UTC)
        if any(
            asset.kind == AssetKind.DEVICE_RAW.value
            and asset.created_at.replace(tzinfo=UTC) + timedelta(seconds=upload_ttl) > now
            for asset in assets
        ):
            raise HTTPException(409, "음성 업로드 대기 시간이 끝난 후 다시 시도해주세요")
        try:
            for asset in assets:
                await request.app.state.container.storage.delete(asset.uri)
        except (StorageError, OSError, BotoCoreError, ClientError) as exc:
            raise HTTPException(503, "음성 파일 삭제를 잠시 후 다시 시도해주세요") from exc

        for model in (
            QuestionTtsGrant, AcousticAnalysisRun, AcousticFeature, ChangeSignal,
            ExtractionEvidence, HealthExtraction, RepeatEvent, Transcript, AudioAsset,
        ):
            await session.execute(delete(model).where(model.call_id.in_(call_ids)))
        await session.execute(delete(CallRecord).where(CallRecord.id.in_(call_ids)))
        # Reports and baselines can contain summaries derived from the deleted calls.
        for model in (Report, Baseline):
            await session.execute(delete(model).where(model.parent_id.in_(parent_ids)))
        owned_family_ids = select(Family.id).where(Family.created_by == user.id)
        removed_member_ids = select(FamilyMember.id).where(
            or_(FamilyMember.user_id == user.id, FamilyMember.family_id.in_(owned_family_ids))
        )
        await session.execute(
            delete(Invitation).where(Invitation.member_id.in_(removed_member_ids))
        )
        await session.execute(delete(FamilyMember).where(FamilyMember.id.in_(removed_member_ids)))
        await session.execute(delete(Family).where(Family.created_by == user.id))
        await session.execute(delete(ParentProfile).where(ParentProfile.parent_id == user.id))
        for model in (ConsentRecord, QuestionTtsGrant, Device, RefreshSession):
            await session.execute(delete(model).where(model.user_id == user.id))
        if user.phone:
            await session.execute(delete(OtpChallenge).where(OtpChallenge.phone == user.phone))
        await session.execute(delete(User).where(User.id == user.id))
        await session.flush()
        if identity is not None and payload is not None and client_secret is not None:
            try:
                await revoke_apple_authorization(
                    container.settings, container.apple_identity, code=payload.authorization_code,
                    subject=identity.subject, nonce=identity.nonce, client_secret=client_secret,
                )
            except (AppleIdentityError, AppleServiceError) as exc:
                raise HTTPException(
                    503, "Apple 권한 해제를 완료하지 못했어요. 다시 인증 후 삭제해주세요"
                ) from exc
        await session.commit()
        return Response(status_code=204)
