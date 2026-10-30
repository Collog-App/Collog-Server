from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent import CONSENT_ITEMS, CONSENT_VERSION
from app.models import (
    ConsentDecision,
    ConsentRecord,
    Family,
    FamilyMember,
    Invitation,
    User,
)


async def family_for_child(session: AsyncSession, child_id: str) -> Family | None:
    return await session.scalar(select(Family).where(Family.created_by == child_id))


async def family_for_parent(session: AsyncSession, parent_id: str) -> Family | None:
    """초대를 수락해 구성원이 된 부모의 가족.

    자녀는 가족을 만든 사람이라 `Family.created_by`로 찾지만, 부모는 `FamilyMember`로만
    연결되므로 그쪽을 거쳐야 한다.
    """
    return await session.scalar(
        select(Family)
        .join(FamilyMember, FamilyMember.family_id == Family.id)
        .outerjoin(Invitation, Invitation.member_id == FamilyMember.id)
        .where(FamilyMember.user_id == parent_id)
        .order_by(Invitation.accepted_at.desc().nulls_last(), FamilyMember.invited_at.desc())
        .limit(1)
    )


async def family_for_user(session: AsyncSession, user_id: str) -> Family | None:
    """역할과 상관없이 사용자가 속한 가족.

    한 사람은 한 가족에만 속한다. 예전 데이터에서 둘 다 있으면 수락한 가족을 먼저 본다.
    """
    return await family_for_parent(session, user_id) or await family_for_child(session, user_id)


def connected_family_ids(user_id: str) -> Select[tuple[str]]:
    """사용자가 만들었거나 초대를 수락해 들어간 가족의 ID."""
    return (
        select(Family.id)
        .outerjoin(FamilyMember, FamilyMember.family_id == Family.id)
        .where(or_(Family.created_by == user_id, FamilyMember.user_id == user_id))
    )


async def is_family_member(session: AsyncSession, family: Family, user_id: str) -> bool:
    if family.created_by == user_id:
        return True
    member = await session.scalar(
        select(FamilyMember.id).where(
            FamilyMember.family_id == family.id, FamilyMember.user_id == user_id
        ).limit(1)
    )
    return member is not None


async def family_of(session: AsyncSession, user: User) -> Family | None:
    """응답의 `familyId`. 부모 역할 계정의 빈 가족을 숨기는 것은 iOS 호환 때문이다."""
    if user.role == "PARENT":
        joined = await family_for_parent(session, user.id)
        if joined is not None:
            return joined
        owned = await family_for_child(session, user.id)
        if owned is None:
            return None
        member = await session.scalar(select(FamilyMember.id).where(
            FamilyMember.family_id == owned.id,
            FamilyMember.user_id.is_not(None),
            FamilyMember.user_id != user.id,
        ).limit(1))
        return owned if member is not None else None
    return await family_for_child(session, user.id) or await family_for_parent(session, user.id)


async def latest_consent(session: AsyncSession, parent_id: str) -> ConsentRecord | None:
    return await session.scalar(
        select(ConsentRecord)
        .where(ConsentRecord.user_id == parent_id)
        .order_by(ConsentRecord.agreed_at.desc())
        .limit(1)
    )


def consent_is_current(record: ConsentRecord | None, version: str = CONSENT_VERSION) -> bool:
    return record is not None and record.document_version == version and (
        record.decision == ConsentDecision.DENIED.value
        or set(CONSENT_ITEMS).issubset(record.agreed_items)
    )


async def has_consent(
    session: AsyncSession, parent_id: str, version: str = CONSENT_VERSION
) -> bool:
    record = await latest_consent(session, parent_id)
    return consent_is_current(record, version) and record.decision == ConsentDecision.GRANTED.value


async def participants_consented(
    session: AsyncSession, parent_id: str, child_id: str, version: str = CONSENT_VERSION
) -> bool:
    return await has_consent(session, parent_id, version) and await has_consent(
        session, child_id, version
    )


async def latest_invitation(session: AsyncSession, member_id: str) -> Invitation | None:
    return await session.scalar(
        select(Invitation)
        .where(Invitation.member_id == member_id)
        .order_by(Invitation.created_at.desc())
        .limit(1)
    )


async def consent_status(
    session: AsyncSession, user_id: str, version: str = CONSENT_VERSION
) -> str:
    consent = await latest_consent(session, user_id)
    if consent_is_current(consent, version):
        return "CONSENT_GRANTED" if consent.decision == "GRANTED" else "CONSENT_DENIED"
    return "AWAITING_CONSENT"


async def derived_member_status(
    session: AsyncSession, member: FamilyMember, invitation: Invitation | None = None,
    version: str = CONSENT_VERSION,
) -> str:
    if member.user_id:
        return await consent_status(session, member.user_id, version)
    invitation = invitation or await latest_invitation(session, member.id)
    now = datetime.now(UTC)
    if invitation and invitation.expires_at.replace(tzinfo=UTC) <= now:
        return "INVITE_EXPIRED"
    return "AWAITING_CONSENT"


async def ensure_same_family(session: AsyncSession, user_id: str, other_id: str) -> None:
    shared_family = await session.scalar(
        select(Family.id)
        .outerjoin(FamilyMember)
        .where(
            Family.id.in_(connected_family_ids(user_id)),
            or_(Family.created_by == other_id, FamilyMember.user_id == other_id),
        )
        .limit(1)
    )
    if shared_family is None or user_id == other_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")


async def ensure_subject_access(session: AsyncSession, user: User, subject_id: str) -> None:
    """`/parents/{id}/...`의 id를 대상 사용자로 읽는다. 본인 또는 같은 가족 구성원만 허용한다."""
    if user.id == subject_id:
        return
    await ensure_same_family(session, user.id, subject_id)
