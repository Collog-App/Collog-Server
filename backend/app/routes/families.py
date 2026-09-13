from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status
from sqlalchemy import or_, select, update

from app.models import Family, FamilyMember, Invitation, User, UserRole
from app.routes.shared import aware, settings_from
from app.schemas import InvitationAccept, InvitationCreate
from app.security import CurrentUser, SessionDep, require_role
from app.services.domain import (
    derived_member_status,
    family_for_child,
    has_consent,
    latest_invitation,
)

router = APIRouter()


@router.get("/families", tags=["Family"])
async def get_families(user: CurrentUser, session: SessionDep) -> dict:
    membership = select(FamilyMember.family_id).where(FamilyMember.user_id == user.id)
    owned = Family.created_by == user.id
    if user.role == UserRole.PARENT:
        populated = select(FamilyMember.id).where(
            FamilyMember.family_id == Family.id,
            FamilyMember.user_id.is_not(None),
            FamilyMember.user_id != user.id,
        ).exists()
        owned = owned & populated
    rows = await session.execute(
        select(Family, User.name)
        .join(User, User.id == Family.created_by)
        .where(or_(owned, Family.id.in_(membership)))
        .order_by(Family.created_at, Family.id)
    )
    families = [{"familyId": family.id, "name": f"{name}의 가족"} for family, name in rows]
    return {"families": families}


@router.post("/families/{familyId}/invitations", status_code=201, tags=["Family"])
async def create_invitation(
    family_id: Annotated[str, Path(alias="familyId")],
    payload: InvitationCreate,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    require_role(user, UserRole.CHILD)
    family = await family_for_child(session, user.id)
    if family is None or family.id != family_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")
    member = FamilyMember(
        family_id=family.id,
        name=payload.name,
        relation=payload.relation,
        invited_at=datetime.now(UTC),
    )
    session.add(member)
    await session.flush()
    code = await unique_invitation_code(session)
    invitation = Invitation(
        member_id=member.id,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(invitation)
    await session.commit()
    return invitation_dict(invitation)


async def unique_invitation_code(session: SessionDep) -> str:
    for _ in range(20):
        code = f"{secrets.randbelow(1_000_000):06d}"
        if await session.scalar(select(Invitation.id).where(Invitation.code == code)) is None:
            return code
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "초대 코드를 만들지 못했습니다")


def invitation_dict(invitation: Invitation) -> dict:
    status_value = (
        "ACCEPTED"
        if invitation.accepted_at
        else ("EXPIRED" if aware(invitation.expires_at) <= datetime.now(UTC) else "PENDING")
    )
    return {
        "invitationId": invitation.id,
        "code": invitation.code,
        "shareText": f"콜록 가족 초대 코드 {invitation.code}를 앱에 입력해주세요.",
        "expiresAt": aware(invitation.expires_at),
        "status": status_value,
    }


@router.get("/families/{familyId}/members", tags=["Family"])
async def get_members(
    family_id: Annotated[str, Path(alias="familyId")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    # 구성원 목록은 그 가족에 속한 사람이면 볼 수 있다. 자녀는 가족을 만든 사람으로,
    # 부모는 초대를 수락한 구성원으로 확인한다.
    family = await session.get(Family, family_id)
    membership = await session.scalar(
        select(FamilyMember.id).where(
            FamilyMember.family_id == family_id,
            FamilyMember.user_id == user.id,
        )
    )
    if family is None or (family.created_by != user.id and membership is None):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")
    members = list(
        await session.scalars(select(FamilyMember).where(FamilyMember.family_id == family_id))
    )
    output = []
    for member in members:
        member_user = await session.get(User, member.user_id) if member.user_id else None
        invitation = await latest_invitation(session, member.id)
        member_status = await derived_member_status(
            session, member, invitation, settings_from(request).consent_document_version
        )
        output.append(
            {
                "memberId": member.id,
                "userId": member.user_id,
                "name": member.name,
                "relation": member.relation,
                "role": member_user.role if member_user else UserRole.PARENT.value,
                "status": member_status,
                "canRegisterConditions": (
                    member_status == "CONSENT_GRANTED"
                    and member_user is not None
                    and member_user.role == UserRole.PARENT.value
                ),
                "invitedAt": aware(member.invited_at),
                "expiresAt": aware(invitation.expires_at) if invitation else None,
                "invitation": (
                    invitation_dict(invitation)
                    if invitation and family.created_by == user.id
                    else None
                ),
            }
        )
    owner = await session.get(User, family.created_by)
    if owner:
        owner_consent = await has_consent(
            session, owner.id, settings_from(request).consent_document_version
        )
        output.append(
            {
                "memberId": owner.id,
                "userId": owner.id,
                "name": owner.name,
                "relation": owner.role,
                "role": owner.role,
                "status": (
                    "ACTIVE" if owner.role == UserRole.CHILD.value
                    else "CONSENT_GRANTED" if owner_consent else "AWAITING_CONSENT"
                ),
                "canRegisterConditions": owner.role == UserRole.PARENT.value and owner_consent,
                "invitedAt": None,
                "expiresAt": None,
                "invitation": None,
            }
        )
    return {
        "members": output,
        "canInvite": family.created_by == user.id and user.role == UserRole.CHILD.value,
    }


@router.post("/invitations/{invitationId}/resend", status_code=201, tags=["Family"])
async def resend_invitation(
    invitation_id: Annotated[str, Path(alias="invitationId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    require_role(user, UserRole.CHILD)
    old = await session.get(Invitation, invitation_id)
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "초대를 찾을 수 없습니다")
    member = await session.scalar(
        select(FamilyMember).where(FamilyMember.id == old.member_id).with_for_update()
    )
    family = await family_for_child(session, user.id)
    if member is None or family is None or member.family_id != family.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")
    if member.user_id is not None or old.accepted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 수락한 초대입니다")
    await session.execute(
        update(Invitation)
        .where(Invitation.member_id == member.id, Invitation.accepted_at.is_(None))
        .values(expires_at=datetime.now(UTC))
    )
    invitation = Invitation(
        member_id=member.id,
        code=await unique_invitation_code(session),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(invitation)
    await session.commit()
    return invitation_dict(invitation)


@router.post("/invitations/accept", tags=["Family"])
async def accept_invitation(
    payload: InvitationAccept, request: Request, user: CurrentUser, session: SessionDep
) -> dict:
    require_role(user, UserRole.PARENT)
    invitation = await session.scalar(
        select(Invitation)
        .where(Invitation.code == payload.code)
        .order_by(Invitation.created_at.desc())
    )
    if invitation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "초대 코드를 찾을 수 없습니다")
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    member = await session.scalar(
        select(FamilyMember).where(FamilyMember.id == invitation.member_id).with_for_update()
    )
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "가족 구성원을 찾을 수 없습니다")
    if member.user_id and member.user_id != user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 다른 계정이 수락한 초대입니다")
    await session.refresh(invitation)
    if member.user_id == user.id and invitation.accepted_at is not None:
        return {
            "familyId": member.family_id,
            "memberId": member.id,
            "status": await derived_member_status(
                session, member, invitation, settings_from(request).consent_document_version
            ),
        }
    if aware(invitation.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, "만료된 초대예요. 다시 초대를 요청해주세요")
    existing = await session.scalar(
        select(FamilyMember.id).where(
            FamilyMember.family_id == member.family_id,
            FamilyMember.user_id == user.id,
            FamilyMember.id != member.id,
        )
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입한 가족입니다")
    assigned = await session.execute(
        update(FamilyMember)
        .where(FamilyMember.id == member.id, FamilyMember.user_id.is_(None))
        .values(user_id=user.id)
    )
    if assigned.rowcount != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 수락한 초대입니다")
    invitation.accepted_at = datetime.now(UTC)
    await session.commit()
    return {"familyId": member.family_id, "memberId": member.id, "status": "AWAITING_CONSENT"}
