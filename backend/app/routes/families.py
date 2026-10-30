from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from sqlalchemy import delete, or_, select, update

from app.models import Family, FamilyMember, Invitation, User, UserRole
from app.routes.shared import aware, settings_from
from app.schemas import InvitationAccept, InvitationCreate
from app.security import CurrentUser, SessionDep
from app.services.domain import (
    connected_family_ids,
    consent_status,
    derived_member_status,
    family_for_user,
    is_family_member,
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


@router.post("/families", tags=["Family"])
async def create_family(response: Response, user: CurrentUser, session: SessionDep) -> dict:
    """가족이 없는 사람의 첫 초대 전에 가족을 만든다. 이미 있으면 그 가족을 준다."""
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    family = await family_for_user(session, user.id)
    if family is None:
        family = Family(created_by=user.id)
        session.add(family)
        await session.commit()
        response.status_code = status.HTTP_201_CREATED
    return {"familyId": family.id}


@router.post("/families/{familyId}/invitations", status_code=201, tags=["Family"])
async def create_invitation(
    family_id: Annotated[str, Path(alias="familyId")],
    payload: InvitationCreate,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    family = await session.get(Family, family_id)
    if family is None or not await is_family_member(session, family, user.id):
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
    # 구성원 목록은 그 가족에 속한 사람(만든 사람 또는 초대를 수락한 구성원)이면 볼 수 있다.
    family = await session.get(Family, family_id)
    if family is None or not await is_family_member(session, family, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "가족 접근 권한이 없습니다")
    version = settings_from(request).consent_document_version
    members = list(
        await session.scalars(select(FamilyMember).where(FamilyMember.family_id == family_id))
    )
    output = []
    for member in members:
        member_user = await session.get(User, member.user_id) if member.user_id else None
        invitation = await latest_invitation(session, member.id)
        member_status = await derived_member_status(session, member, invitation, version)
        output.append(
            {
                "memberId": member.id,
                "userId": member.user_id,
                "name": member.name,
                "relation": member.relation,
                "role": member_user.role if member_user else UserRole.PARENT.value,
                "status": member_status,
                "canRegisterConditions": member_status == "CONSENT_GRANTED",
                "invitedAt": aware(member.invited_at),
                "expiresAt": aware(invitation.expires_at) if invitation else None,
                "acceptedAt": (
                    aware(invitation.accepted_at)
                    if member.user_id and invitation and invitation.accepted_at
                    else None
                ),
                "consentGranted": member_status == "CONSENT_GRANTED",
                "isCreator": False,
                # 누구나 초대할 수 있으므로 대기 중 초대 코드도 구성원 모두에게 보인다.
                "invitation": invitation_dict(invitation) if invitation else None,
            }
        )
    owner = await session.get(User, family.created_by)
    if owner:
        owner_status = await consent_status(session, owner.id, version)
        output.append(
            {
                "memberId": owner.id,
                "userId": owner.id,
                "name": owner.name,
                "relation": "",
                "role": owner.role,
                "status": owner_status,
                "canRegisterConditions": owner_status == "CONSENT_GRANTED",
                "invitedAt": None,
                "expiresAt": None,
                "acceptedAt": aware(family.created_at),
                "consentGranted": owner_status == "CONSENT_GRANTED",
                "isCreator": True,
                "invitation": None,
            }
        )
    return {"members": output, "canInvite": True}


@router.post("/invitations/{invitationId}/resend", status_code=201, tags=["Family"])
async def resend_invitation(
    invitation_id: Annotated[str, Path(alias="invitationId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    old = await session.get(Invitation, invitation_id)
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "초대를 찾을 수 없습니다")
    member = await session.scalar(
        select(FamilyMember).where(FamilyMember.id == old.member_id).with_for_update()
    )
    family = await session.get(Family, member.family_id) if member else None
    if member is None or family is None or not await is_family_member(session, family, user.id):
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
    version = settings_from(request).consent_document_version
    if member.user_id == user.id and invitation.accepted_at is not None:
        return {
            "familyId": member.family_id,
            "memberId": member.id,
            "status": await derived_member_status(session, member, invitation, version),
        }
    if aware(invitation.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, "만료된 초대예요. 다시 초대를 요청해주세요")
    family = await session.get(Family, member.family_id)
    existing = await session.scalar(
        select(FamilyMember.id).where(
            FamilyMember.family_id == member.family_id,
            FamilyMember.user_id == user.id,
            FamilyMember.id != member.id,
        )
    )
    if family is None or family.created_by == user.id or existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입한 가족입니다")
    # 한 사람은 한 가족에만 속한다. 자기 혼자뿐인 가족은 정리하고, 다른 사람이 있으면 막는다.
    current = list(await session.scalars(
        select(Family).where(
            Family.id.in_(connected_family_ids(user.id)), Family.id != member.family_id
        )
    ))
    for other in current:
        rows = list(await session.scalars(
            select(FamilyMember).where(FamilyMember.family_id == other.id).with_for_update()
        ))
        if {other.created_by, *(row.user_id for row in rows if row.user_id)} - {user.id}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "이미 다른 가족에 속해 있어요. 지금 가족에서 나온 뒤 다시 시도해주세요",
            )
    for other in current:
        solo_members = select(FamilyMember.id).where(FamilyMember.family_id == other.id)
        await session.execute(delete(Invitation).where(Invitation.member_id.in_(solo_members)))
        await session.execute(delete(FamilyMember).where(FamilyMember.family_id == other.id))
        await session.execute(delete(Family).where(Family.id == other.id))
    assigned = await session.execute(
        update(FamilyMember)
        .where(FamilyMember.id == member.id, FamilyMember.user_id.is_(None))
        .values(user_id=user.id)
    )
    if assigned.rowcount != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 수락한 초대입니다")
    invitation.accepted_at = datetime.now(UTC)
    await session.commit()
    return {
        "familyId": member.family_id,
        "memberId": member.id,
        "status": await consent_status(session, user.id, version),
    }
