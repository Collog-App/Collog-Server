from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AssetKind, AssetStatus, AudioAsset, CallRecord, CallState
from tests.conftest import auth
from tests.test_api_flow import onboard_family


def create_call(client: TestClient) -> tuple[str, str, str]:
    child_token, _, parent_token, parent = onboard_family(client)
    response = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert response.status_code == 201
    return response.json()["callId"], child_token, parent_token


def test_abandoned_raw_upload_does_not_block_egress_analysis(client: TestClient) -> None:
    call_id, child_token, parent_token = create_call(client)
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 200
    container = client.app.state.container

    async def abandon_upload() -> None:
        old = datetime.now(UTC) - timedelta(hours=2)
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            call.state = CallState.ENDED.value
            call.ended_at = old
            assets = list(
                await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            )
            for asset in assets:
                asset.status = AssetStatus.FAILED.value
                if asset.kind == AssetKind.WEBRTC_EGRESS_PARENT.value:
                    await container.storage.write(container.storage.object_key(asset.uri), b"hello")
                    asset.status = AssetStatus.UPLOADED.value
                    asset.uploaded_at = datetime.now(UTC)
            session.add(
                AudioAsset(
                    call_id=call_id,
                    kind=AssetKind.DEVICE_RAW.value,
                    uri=container.storage.object_uri(f"calls/{call_id}/missing.wav"),
                    created_at=old,
                )
            )
            await session.commit()

    client.portal.call(abandon_upload)
    client.portal.call(container.pipeline.process_pending)
    result = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert result["state"] == "ANALYSIS_EXCLUDED"


def test_only_expired_processing_claims_are_released(client: TestClient) -> None:
    call_id, _, _ = create_call(client)
    container = client.app.state.container

    async def claim(age: int) -> None:
        async with container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            call.state = CallState.PROCESSING.value
            call.processing_claimed_at = datetime.now(UTC) - timedelta(seconds=age)
            await session.commit()

    client.portal.call(claim, 0)
    assert client.portal.call(container.pipeline.release_stale_claims) == 0
    client.portal.call(claim, container.settings.processing_lease_seconds + 1)
    assert client.portal.call(container.pipeline.release_stale_claims) == 1
