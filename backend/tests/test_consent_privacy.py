from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import CallRecord, ConsentRecord
from app.services.deepgram import DeepgramSttGateway
from app.services.tts import ElevenLabsDirectTtsGateway, create_question_tts_gateway
from tests.conftest import auth
from tests.test_api_flow import onboard_family


def test_child_decline_allows_unrecorded_call_and_blocks_tts(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    document = client.get("/v1/consents/document").json()
    declined = client.post(
        "/v1/consents",
        headers=auth(child_token),
        json={
            "documentVersion": document["version"],
            "decision": "DENY",
            "scrolledToEnd": False,
            "agreedItems": [],
        },
    )
    assert declined.status_code == 201
    assert declined.json()["isCurrent"] is True
    call = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert call.status_code == 201
    assert call.json()["recordingEnabled"] is False
    call_id = call.json()["callId"]
    question = call.json()["questions"][0]
    assert question["ttsMode"] == "IOS_LOCAL"
    token = client.post(
        f"/v1/calls/{call_id}/questions/{question['questionId']}/tts-token",
        headers=auth(child_token),
    )
    assert token.status_code == 403
    accepted = client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    assert accepted.status_code == 200
    assert accepted.json()["recordingEnabled"] is False
    assert accepted.json()["rawCaptureRequired"] is False


@pytest.mark.parametrize("outdated", [True, False])
def test_old_or_incomplete_grant_requires_new_permission(
    client: TestClient, outdated: bool
) -> None:
    child_token, child, _, parent = onboard_family(client)

    async def change_record() -> None:
        async with client.app.state.container.database.sessions() as session:
            record = await session.scalar(
                select(ConsentRecord).where(ConsentRecord.user_id == child["id"])
            )
            if outdated:
                record.document_version = "2026-08-01.v3"
            else:
                record.agreed_items = ["CALL_RECORDING"]
            await session.commit()

    client.portal.call(change_record)
    assert client.get("/v1/consents/me", headers=auth(child_token)).json()["isCurrent"] is False
    call = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert call.status_code == 201
    assert call.json()["recordingEnabled"] is False


def test_accept_rechecks_consent_and_queued_analysis_stops(client: TestClient, monkeypatch) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    call = client.post(
        "/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]}
    ).json()
    assert call["recordingEnabled"] is True

    async def expire_consent() -> None:
        async with client.app.state.container.database.sessions() as session:
            record = await session.scalar(
                select(ConsentRecord).where(ConsentRecord.user_id == child["id"])
            )
            record.document_version = "old"
            await session.commit()

    client.portal.call(expire_consent)
    accepted = client.post(f"/v1/calls/{call['callId']}/accept", headers=auth(parent_token))
    assert accepted.json()["recordingEnabled"] is False

    async def seed_queued() -> None:
        async with client.app.state.container.database.sessions() as session:
            stored = await session.get(CallRecord, call["callId"])
            stored.recording_enabled = True
            stored.ended_at = datetime.now(UTC)
            stored.state = "ENDED"
            await session.commit()

    client.portal.call(seed_queued)
    pipeline = client.app.state.container.pipeline
    assert client.portal.call(pipeline.processing_allowed, call["callId"]) is False


async def test_deepgram_excludes_model_improvement(client: TestClient, monkeypatch) -> None:
    settings = client.app.state.container.settings.model_copy(update={"deepgram_api_key": "test"})
    gateway = DeepgramSttGateway(settings)

    async def respond(client, method, url, **kwargs):
        assert kwargs["params"]["mip_opt_out"] is True
        return httpx.Response(
            200,
            json={
                "results": {"channels": [{"alternatives": [{"transcript": "", "words": []}]}]},
            },
        )

    monkeypatch.setattr("app.services.deepgram.request_with_retry", respond)
    await gateway.transcribe(b"audio", "audio/wav", "PARENT")


def test_unapproved_provider_blocks_queued_processing(client: TestClient) -> None:
    child_token, _, _, parent = onboard_family(client)
    call = client.post(
        "/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]}
    ).json()
    pipeline = client.app.state.container.pipeline
    pipeline.settings = pipeline.settings.model_copy(
        update={
            "mock_external_services": False,
            "gemini_data_processing_approved": False,
        }
    )
    assert client.portal.call(pipeline.processing_allowed, call["callId"]) is False


def test_consent_change_during_call_requires_ending_call(client: TestClient) -> None:
    child_token, _, _, parent = onboard_family(client)
    client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    document = client.get("/v1/consents/document").json()
    response = client.post(
        "/v1/consents",
        headers=auth(child_token),
        json={
            "documentVersion": document["version"],
            "decision": "DENY",
            "scrolledToEnd": False,
            "agreedItems": [],
        },
    )
    assert response.status_code == 409
    assert client.get("/v1/consents/me", headers=auth(child_token)).json()["status"] == "GRANTED"


@pytest.mark.parametrize("approved", [False, True])
def test_remote_voice_requires_provider_approval(client: TestClient, approved: bool) -> None:
    container = client.app.state.container
    settings = container.settings.model_copy(
        update={
            "mock_external_services": False,
            "question_tts_provider": "elevenlabs_direct",
            "elevenlabs_api_key": "test-key",
            "elevenlabs_voice_id": "test-voice",
            "elevenlabs_data_processing_approved": approved,
        }
    )
    gateway = create_question_tts_gateway(settings, container.storage)
    assert isinstance(gateway, ElevenLabsDirectTtsGateway) is approved
