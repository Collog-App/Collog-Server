from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.container import AppContainer
from app.models import AssetKind, AssetStatus, AudioAsset, CallRecord, CallState
from app.services.deepgram import SttError
from app.services.pipeline import transient_processing_error
from tests.conftest import auth
from tests.test_pipeline_recovery import create_call


def prepare_audio(client: TestClient) -> tuple[str, AppContainer]:
    call_id, _, parent_token = create_call(client)
    client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    container = client.app.state.container

    async def prepare() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            call.state = CallState.ENDED.value
            call.ended_at = datetime.now(UTC) - timedelta(minutes=5)
            assets = list(
                await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            )
            for asset in assets:
                asset.status = AssetStatus.FAILED.value
                if asset.kind == AssetKind.WEBRTC_EGRESS_PARENT.value:
                    await container.storage.write(container.storage.object_key(asset.uri), b"hello")
                    asset.status = AssetStatus.UPLOADED.value
                    asset.uploaded_at = datetime.now(UTC)
            await session.commit()

    client.portal.call(prepare)
    return call_id, container


def test_transient_stt_failure_preserves_audio_and_retries(client: TestClient) -> None:
    call_id, container = prepare_audio(client)
    original = container.pipeline.stt.transcribe
    container.pipeline.stt.transcribe = AsyncMock(side_effect=TimeoutError())
    client.portal.call(container.pipeline.process, call_id)

    async def inspect_and_release() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            assert call.state == CallState.ENDED.value
            assert call.processing_attempts == 1
            assert call.processing_retry_at is not None
            asset = await session.scalar(
                select(AudioAsset).where(
                    AudioAsset.call_id == call_id,
                    AudioAsset.kind == AssetKind.WEBRTC_EGRESS_PARENT.value,
                )
            )
            assert await container.storage.exists(asset.uri)
            call.processing_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    client.portal.call(container.pipeline.process, call_id)
    assert container.pipeline.stt.transcribe.await_count == 1
    client.portal.call(inspect_and_release)
    container.pipeline.stt.transcribe = original
    client.portal.call(container.pipeline.process, call_id)

    async def verify() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            assert call.state == CallState.ANALYSIS_EXCLUDED.value
            assert call.processing_attempts == 2
            assert call.processing_error is None
            assert call.raw_audio_purged_at is not None

    client.portal.call(verify)


@pytest.mark.parametrize("error", [SttError("Invalid audio"), TimeoutError()])
def test_permanent_error_or_exhausted_retry_purges_audio(
    client: TestClient, error: Exception
) -> None:
    call_id, container = prepare_audio(client)

    async def exhaust() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            call.processing_attempts = 2
            await session.commit()

    client.portal.call(exhaust)
    container.pipeline.stt.transcribe = AsyncMock(side_effect=error)
    client.portal.call(container.pipeline.process, call_id)

    async def verify() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            assert call.state == CallState.ANALYSIS_FAILED.value
            assert call.processing_retry_at is None
            assert call.raw_audio_purged_at is not None

    client.portal.call(verify)


@pytest.mark.parametrize("status,expected", [(401, False), (400, False), (429, True), (503, True)])
def test_stt_wrapped_http_error_classification(status: int, expected: bool) -> None:
    request = httpx.Request("POST", "https://example.test")
    error = SttError("Provider failed")
    error.__cause__ = httpx.HTTPStatusError(
        "Provider failed", request=request, response=httpx.Response(status, request=request)
    )
    assert transient_processing_error(error) is expected


def test_revocation_during_stt_failure_cancels_retry(client: TestClient) -> None:
    call_id, container = prepare_audio(client)

    async def revoke_and_fail(*args: object) -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            call.recording_enabled = False
            await session.commit()
        raise TimeoutError()

    container.pipeline.stt.transcribe = AsyncMock(side_effect=revoke_and_fail)
    client.portal.call(container.pipeline.process, call_id)

    async def verify() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            assert call.state == CallState.ANALYSIS_EXCLUDED.value
            assert call.processing_retry_at is None
            assert call.raw_audio_purged_at is not None

    client.portal.call(verify)


def test_expired_retry_audio_is_purged_without_stt(client: TestClient) -> None:
    call_id, container = prepare_audio(client)

    async def expire() -> None:
        async with container.database.sessions() as session:
            assets = await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            for asset in assets:
                asset.uploaded_at = datetime.now(UTC) - timedelta(hours=25)
            await session.commit()

    client.portal.call(expire)
    container.pipeline.stt.transcribe = AsyncMock()
    client.portal.call(container.pipeline.process, call_id)
    assert container.pipeline.stt.transcribe.await_count == 0

    async def verify() -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            assert call.state == CallState.ANALYSIS_FAILED.value
            assert call.raw_audio_purged_at is not None

    client.portal.call(verify)


def test_disabled_call_purges_audio_that_arrives_after_purge(client: TestClient) -> None:
    call_id, container = prepare_audio(client)
    client.portal.call(container.pipeline.purge_call_audio, call_id)

    async def late_audio() -> str:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            call.recording_enabled = False
            asset = await session.scalar(select(AudioAsset).where(AudioAsset.call_id == call_id))
            await container.storage.write(container.storage.object_key(asset.uri), b"late audio")
            await session.commit()
            return asset.uri

    uri = client.portal.call(late_audio)
    client.portal.call(container.pipeline.purge_expired_audio)
    assert not client.portal.call(container.storage.exists, uri)
