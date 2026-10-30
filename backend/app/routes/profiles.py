from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from app.models import ParentProfile
from app.routes.shared import settings_from
from app.schemas import ProfilePut
from app.security import CurrentUser, SessionDep
from app.services.domain import ensure_subject_access, has_consent

router = APIRouter()


@router.get("/parents/{parentId}/profile", tags=["Profile"])
async def get_profile(
    parent_id: Annotated[str, Path(alias="parentId")],
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    await ensure_subject_access(session, user, parent_id)
    profile = await session.get(ParentProfile, parent_id)
    if profile is None:
        return {
            "parentId": parent_id,
            "conditions": [],
            "concerns": None,
            "updatedAt": None,
            "isCompleted": False,
        }
    return profile_view(profile)


@router.put("/parents/{parentId}/profile", tags=["Profile"])
async def put_profile(
    parent_id: Annotated[str, Path(alias="parentId")],
    payload: ProfilePut,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    # 누구나 자기 프로필을 갖고, 같은 가족 구성원도 고칠 수 있다. 저장은 대상 본인이 동의한 뒤에만.
    await ensure_subject_access(session, user, parent_id)
    if not await has_consent(session, parent_id, settings_from(request).consent_document_version):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "프로필 주인의 분석 동의가 완료되어야 등록할 수 있어요"
        )
    profile = await session.get(ParentProfile, parent_id)
    if profile is None:
        profile = ParentProfile(parent_id=parent_id, conditions=payload.conditions)
        session.add(profile)
    else:
        profile.conditions = payload.conditions
        profile.updated_at = datetime.now(UTC)
    # Older clients send only conditions; keep the stored note unless the field is sent.
    if "concerns" in payload.model_fields_set:
        profile.concerns = payload.concerns
    await session.commit()
    return profile_view(profile)


def profile_view(profile: ParentProfile) -> dict:
    return {
        "parentId": profile.parent_id,
        "conditions": profile.conditions,
        "concerns": profile.concerns,
        "updatedAt": profile.updated_at,
        "isCompleted": True,
    }
