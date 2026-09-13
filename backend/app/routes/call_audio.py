from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Request, Response, status
from sqlalchemy import select

from app.models import AssetKind, AssetStatus, AudioAsset, CallRecord, CallState
from app.routes.shared import aware, settings_from
from app.schemas import RawAudioComplete, RawAudioUploadRequest
from app.security import CurrentUser, SessionDep
from app.services.storage import LocalStorage

router = APIRouter()


@router.post("/calls/{callId}/raw-audio/upload-url", tags=["Call"])
async def raw_audio_upload_url(
    call_id: Annotated[str, Path(alias="callId")],
    payload: RawAudioUploadRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.scalar(
        select(CallRecord).where(CallRecord.id == call_id).with_for_update()
    )
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다")
    if user.id != call.parent_id or not call.recording_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "원시 오디오 업로드 권한이 없습니다")
    if call.state not in {CallState.ACTIVE.value, CallState.ENDED.value}:
        raise HTTPException(status.HTTP_409_CONFLICT, "오디오 업로드 시간이 지났습니다")
    if call.ended_at and (datetime.now(UTC) - aware(call.ended_at)).total_seconds() > (
        settings_from(request).upload_url_ttl_seconds
    ):
        raise HTTPException(status.HTTP_410_GONE, "오디오 업로드 시간이 지났습니다")
    existing = await session.scalar(select(AudioAsset).where(
        AudioAsset.call_id == call.id,
        AudioAsset.kind == AssetKind.DEVICE_RAW.value,
        AudioAsset.status == AssetStatus.PENDING.value,
        AudioAsset.created_at >= datetime.now(UTC) - timedelta(
            seconds=settings_from(request).upload_url_ttl_seconds
        ),
    ).order_by(AudioAsset.created_at.desc()).limit(1))
    if existing is not None:
        upload_url = await request.app.state.container.storage.create_upload_url(
            request.app.state.container.storage.object_key(existing.uri), existing.content_type
        )
        return {
            "uploadUrl": upload_url,
            "assetId": existing.id,
            "expiresIn": max(0, settings_from(request).upload_url_ttl_seconds - int(
                (datetime.now(UTC) - aware(existing.created_at)).total_seconds()
            )),
        }
    key = f"calls/{call.id}/raw/parent-{random.randrange(10**10):010d}.wav"
    asset = AudioAsset(
        call_id=call.id,
        kind=AssetKind.DEVICE_RAW.value,
        uri=request.app.state.container.storage.object_uri(key),
        content_type=payload.content_type,
        duration_sec=payload.duration_sec,
        sample_rate=payload.sample_rate,
    )
    session.add(asset)
    await session.commit()
    upload_url = await request.app.state.container.storage.create_upload_url(
        key, payload.content_type
    )
    return {
        "uploadUrl": upload_url,
        "assetId": asset.id,
        "expiresIn": settings_from(request).upload_url_ttl_seconds,
    }


@router.put("/uploads/{encoded_key:path}", include_in_schema=False)
async def local_upload(
    encoded_key: str,
    request: Request,
    expires: int,
    signature: str,
) -> Response:
    storage = request.app.state.container.storage
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "로컬 업로드가 비활성화되어 있습니다")
    if not storage.verify_upload(encoded_key, expires, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "업로드 URL이 만료되었거나 잘못되었습니다")
    body = await request.body()
    await storage.write(encoded_key, body)
    return Response(status_code=204)


@router.post("/calls/{callId}/raw-audio/complete", status_code=202, tags=["Call"])
async def raw_audio_complete(
    call_id: Annotated[str, Path(alias="callId")],
    payload: RawAudioComplete,
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    session: SessionDep,
) -> dict:
    call = await session.scalar(
        select(CallRecord).where(CallRecord.id == call_id).with_for_update()
    )
    asset = await session.get(AudioAsset, payload.asset_id)
    if call is None or asset is None or asset.call_id != call.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "오디오 자산을 찾을 수 없습니다")
    if user.id != call.parent_id or asset.kind != AssetKind.DEVICE_RAW.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "오디오 자산 접근 권한이 없습니다")
    if not call.recording_enabled:
        await session.rollback()
        await request.app.state.container.pipeline.purge_call_audio(call_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이번 통화의 녹음과 분석이 중단되었습니다")
    if asset.uploaded_at is not None and asset.status in {
        AssetStatus.UPLOADED.value,
        AssetStatus.PURGED.value,
    }:
        return {"status": "COMPLETED" if asset.status == AssetStatus.PURGED.value else "QUEUED"}
    if asset.status != AssetStatus.PENDING.value or call.state not in {
        CallState.ACTIVE.value,
        CallState.ENDED.value,
    }:
        raise HTTPException(status.HTTP_409_CONFLICT, "오디오 업로드 시간이 지났습니다")
    if (datetime.now(UTC) - aware(asset.created_at)).total_seconds() > settings_from(
        request
    ).upload_url_ttl_seconds:
        raise HTTPException(status.HTTP_410_GONE, "오디오 업로드 URL이 만료되었습니다")
    if not await request.app.state.container.storage.exists(asset.uri):
        raise HTTPException(status.HTTP_409_CONFLICT, "오디오 업로드가 완료되지 않았습니다")
    asset.status = AssetStatus.UPLOADED.value
    asset.uploaded_at = datetime.now(UTC)
    await session.commit()
    background.add_task(request.app.state.container.pipeline.process, call.id)
    return {"status": "QUEUED"}
