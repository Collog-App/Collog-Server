from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from app.models import ParentProfile, UserRole
from app.routes.shared import settings_from
from app.schemas import ProfilePut
from app.security import CurrentUser, SessionDep
from app.services.domain import ensure_child_can_access_parent, ensure_report_access, has_consent

router = APIRouter()


@router.get("/parents/{parentId}/profile", tags=["Profile"])
async def get_profile(
    parent_id: Annotated[str, Path(alias="parentId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    await ensure_report_access(session, user, parent_id)
    profile = await session.get(ParentProfile, parent_id)
    if profile is None:
        return {"parentId": parent_id, "conditions": [], "updatedAt": None, "isCompleted": False}
    return {
        "parentId": profile.parent_id,
        "conditions": profile.conditions,
        "updatedAt": profile.updated_at,
        "isCompleted": True,
    }


@router.put("/parents/{parentId}/profile", tags=["Profile"])
async def put_profile(
    parent_id: Annotated[str, Path(alias="parentId")],
    payload: ProfilePut,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    if user.role == UserRole.CHILD.value:
        await ensure_child_can_access_parent(session, user, parent_id)
    elif user.id != parent_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "프로필 접근 권한이 없습니다")
    if not await has_consent(session, parent_id, settings_from(request).consent_document_version):
        raise HTTPException(status.HTTP_409_CONFLICT, "부모님 동의가 완료되어야 등록할 수 있어요")
    profile = await session.get(ParentProfile, parent_id)
    if profile is None:
        profile = ParentProfile(parent_id=parent_id, conditions=payload.conditions)
        session.add(profile)
    else:
        profile.conditions = payload.conditions
        profile.updated_at = datetime.now(UTC)
    await session.commit()
    return {
        "parentId": profile.parent_id,
        "conditions": profile.conditions,
        "updatedAt": profile.updated_at,
        "isCompleted": True,
    }
