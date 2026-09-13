from __future__ import annotations

import io
import math
import wave
from array import array
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AssetKind, AudioAsset
from tests.conftest import auth, create_user


def tone_wav(seconds: float = 6, sample_rate: int = 16_000, frequency: float = 150) -> bytes:
    samples = array(
        "h",
        (
            round(0.2 * 32767 * math.sin(2 * math.pi * frequency * index / sample_rate))
            for index in range(round(seconds * sample_rate))
        ),
    )
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return output.getvalue()


def onboard_family(client: TestClient):
    child_token, child = create_user(client, "01011112222", "CHILD", "김철수")
    parent_token, parent = create_user(client, "01033334444", "PARENT", "김순자")
    invited = client.post(
        f"/v1/families/{child['familyId']}/invitations",
        headers=auth(child_token),
        json={"name": "어머니", "relation": "MOTHER"},
    )
    assert invited.status_code == 201, invited.text
    accepted = client.post(
        "/v1/invitations/accept",
        headers=auth(parent_token),
        json={"code": invited.json()["code"]},
    )
    assert accepted.status_code == 200, accepted.text
    document = client.get("/v1/consents/document").json()
    consented = client.post(
        "/v1/consents",
        headers=auth(parent_token),
        json={
            "documentVersion": document["version"],
            "decision": "GRANT",
            "scrolledToEnd": True,
            "agreedItems": document["requiredItems"],
        },
    )
    assert consented.status_code == 201, consented.text
    return child_token, child, parent_token, parent


def test_invitation_consent_profile_and_questions(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)
    members = client.get(f"/v1/families/{child['familyId']}/members", headers=auth(child_token))
    assert members.status_code == 200
    assert members.json()["members"][0]["status"] == "CONSENT_GRANTED"

    profile = client.put(
        f"/v1/parents/{parent['id']}/profile",
        headers=auth(child_token),
        json={"conditions": ["HYPERTENSION", "ASTHMA"]},
    )
    assert profile.status_code == 200, profile.text
    questions = client.get(f"/v1/parents/{parent['id']}/daily-questions", headers=auth(child_token))
    assert questions.status_code == 200
    assert questions.json()["source"] == "CONDITION_POOL"
    assert 1 <= len(questions.json()["questions"]) <= 2


def test_call_without_parent_consent_never_records(client: TestClient) -> None:
    child_token, child = create_user(client, "01055556666", "CHILD", "자녀")
    parent_token, parent = create_user(client, "01077778888", "PARENT", "부모")
    invited = client.post(
        f"/v1/families/{child['familyId']}/invitations",
        headers=auth(child_token),
        json={"name": "아버지", "relation": "FATHER"},
    )
    client.post(
        "/v1/invitations/accept",
        headers=auth(parent_token),
        json={"code": invited.json()["code"]},
    )
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201
    assert created.json()["recordingEnabled"] is False
    assert created.json()["recordingDisabledReason"] == "CONSENT_PENDING"
    accepted = client.post(
        f"/v1/calls/{created.json()['callId']}/accept", headers=auth(parent_token)
    )
    assert accepted.status_code == 200
    assert accepted.json()["rawCaptureRequired"] is False


def test_ios_device_registration_triggers_voip_push(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    registered = client.post(
        "/v1/devices",
        headers=auth(parent_token),
        json={
            "platform": "IOS",
            "token": "11" * 32,
            "voipToken": "aa" * 32,
        },
    )
    assert registered.status_code == 201, registered.text

    created = client.post(
        "/v1/calls",
        headers=auth(child_token),
        json={"calleeId": parent["id"]},
    )
    assert created.status_code == 201, created.text

    push_gateway = client.app.state.container.voip_push
    assert len(push_gateway.sent) == 1
    token, push = push_gateway.sent[0]
    assert token == "aa" * 32
    assert push.call_id == created.json()["callId"]
    assert push.payload()["call"]["callUUID"] == created.json()["callId"]
    assert "accessToken" not in push.payload()["call"]


def test_complete_call_pipeline(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    created = client.post(
        "/v1/calls",
        headers=auth(child_token),
        json={"calleeId": parent["id"]},
    )
    assert created.status_code == 201, created.text
    call_id = created.json()["callId"]
    assert created.json()["recordingEnabled"] is True
    assert created.json()["audioConstraints"] == {
        "echoCancellation": True,
        "noiseSuppression": False,
        "autoGainControl": False,
        "dtx": False,
        "audioBitrate": 48000,
        "rawCaptureSampleRate": 16000,
    }

    accepted = client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    assert accepted.status_code == 200, accepted.text
    ended = client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token))
    assert ended.status_code == 200, ended.text

    upload = client.post(
        f"/v1/calls/{call_id}/raw-audio/upload-url",
        headers=auth(parent_token),
        json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
    )
    assert upload.status_code == 200, upload.text
    parsed = urlsplit(upload.json()["uploadUrl"])
    uploaded = client.put(parsed.path + "?" + parsed.query, content=tone_wav())
    assert uploaded.status_code == 204, uploaded.text
    completed = client.post(
        f"/v1/calls/{call_id}/raw-audio/complete",
        headers=auth(parent_token),
        json={"assetId": upload.json()["assetId"]},
    )
    assert completed.status_code == 202

    async def prepare_egress_assets():
        database = client.app.state.container.database
        async with database.sessions() as session:
            assets = list(
                await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            )
        storage = client.app.state.container.storage
        parent_text = (
            "PARENT: 어젯밤에 기침 때문에 두 번 깼어. 그래도 아침 혈압약은 챙겨 먹었고 "
            "오후에는 공원을 삼십 분 정도 천천히 산책했어. 오늘은 잠이 조금 부족해서 "
            "평소보다 졸리지만 다른 불편함은 따로 이야기하지 않았어."
        )
        child_text = "CHILD: 기침 때문에 깨셨군요. 약은 드셨고 산책도 하셨어요?"
        for asset in assets:
            if asset.kind == AssetKind.WEBRTC_EGRESS_PARENT.value:
                await storage.write(asset.uri.removeprefix("local://"), parent_text.encode())
            elif asset.kind == AssetKind.WEBRTC_EGRESS_CHILD.value:
                await storage.write(asset.uri.removeprefix("local://"), child_text.encode())
        return [asset for asset in assets if asset.egress_id]

    egress_assets = client.portal.call(prepare_egress_assets)
    for asset in egress_assets:
        webhook = client.post(
            "/v1/webhooks/livekit",
            json={
                "event": "egress_ended",
                "egress_info": {"egress_id": asset.egress_id, "status": "EGRESS_COMPLETE"},
            },
        )
        assert webhook.status_code == 204, webhook.text

    call = client.get(f"/v1/calls/{call_id}", headers=auth(child_token))
    assert call.status_code == 200
    assert call.json()["state"] == "ANALYZED"
    assert call.json()["parentSpeechSec"] >= 20
    assert call.json()["rawAudioPurgedAt"] is not None

    transcript = client.get(f"/v1/calls/{call_id}/transcript", headers=auth(child_token))
    assert transcript.status_code == 200
    assert {segment["speaker"] for segment in transcript.json()["segments"]} == {
        "PARENT",
        "CHILD",
    }
    extraction = client.get(f"/v1/calls/{call_id}/extraction", headers=auth(child_token))
    assert extraction.status_code == 200
    assert extraction.json()["parseStatus"] == "OK"
    assert "기침" in extraction.json()["symptom"]
    assert extraction.json()["medication"] is not None
    assert extraction.json()["activity"] is not None
    assert extraction.json()["sleep"] is not None
    assert extraction.json()["schemaVersion"] == "v2"
    assert all(item["evidenceSegmentIds"] for item in extraction.json()["facts"])

    acoustics = client.get(f"/v1/calls/{call_id}/acoustic-features", headers=auth(child_token))
    assert acoustics.status_code == 200
    assert len(acoustics.json()["features"]) == 4
    by_metric = {item["metric"]: item for item in acoustics.json()["features"]}
    assert {item["status"] for metric, item in by_metric.items() if metric != "COUGH_EVENTS"} == {
        "OK"
    }
    # 기침 detector는 검증 전이라 파이프라인 끝까지 UNMEASURABLE로 흘러야 한다.
    assert by_metric["COUGH_EVENTS"]["status"] == "UNMEASURABLE"
    assert by_metric["COUGH_EVENTS"]["unmeasurableReason"] == "DETECTOR_NOT_VALIDATED"
    # 품질 게이트 상수를 보정하면 analyzer version이 올라간다. 값 자체를 고정하는 대신
    # 실행한 설정이 응답에 그대로 기록되는지 확인한다.
    assert (
        acoustics.json()["analyzerVersion"]
        == client.app.state.container.settings.acoustic_analyzer_version
    )

    report = client.get(
        f"/v1/parents/{parent['id']}/reports?period=WEEKLY", headers=auth(child_token)
    )
    assert report.status_code == 200, report.text
    assert report.json()["disclaimer"]
    assert report.json()["analyzedCallCount"] == 1
    assert report.json()["repeatObservation"]["count"] == 0


def test_raw_only_development_pipeline_without_egress(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    client.app.state.container.settings.allow_raw_only_analysis = True
    livekit = client.app.state.container.livekit

    async def unexpected_track_lookup(room_name: str, identity: str) -> None:
        del room_name, identity
        raise AssertionError("raw-only mode must not look up an Egress source track")

    async def unexpected_egress_start(
        room_name: str, track_id: str, object_key: str
    ) -> None:
        del room_name, track_id, object_key
        raise AssertionError("raw-only mode must not start Track Egress")

    livekit.find_audio_track_id = unexpected_track_lookup
    livekit.start_track_egress = unexpected_egress_start
    created = client.post(
        "/v1/calls",
        headers=auth(child_token),
        json={"calleeId": parent["id"]},
    )
    call_id = created.json()["callId"]
    accepted = client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    assert accepted.status_code == 200, accepted.text

    upload = client.post(
        f"/v1/calls/{call_id}/raw-audio/upload-url",
        headers=auth(parent_token),
        json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
    )
    parsed = urlsplit(upload.json()["uploadUrl"])
    uploaded = client.put(parsed.path + "?" + parsed.query, content=tone_wav())
    assert uploaded.status_code == 204, uploaded.text

    ended = client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token))
    assert ended.status_code == 200, ended.text
    completed = client.post(
        f"/v1/calls/{call_id}/raw-audio/complete",
        headers=auth(parent_token),
        json={"assetId": upload.json()["assetId"]},
    )
    assert completed.status_code == 202, completed.text

    call = client.get(f"/v1/calls/{call_id}", headers=auth(child_token))
    assert call.json()["state"] == "ANALYZED"
    assert call.json()["rawAudioPurgedAt"] is not None
    transcript = client.get(f"/v1/calls/{call_id}/transcript", headers=auth(child_token))
    assert transcript.status_code == 200, transcript.text
    assert {segment["speaker"] for segment in transcript.json()["segments"]} == {"PARENT"}


def test_track_published_webhook_starts_late_parent_egress(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    livekit = client.app.state.container.livekit

    async def no_track_before_publish(room_name: str, identity: str) -> str | None:
        del room_name, identity
        return None

    livekit.find_audio_track_id = no_track_before_publish
    created = client.post(
        "/v1/calls",
        headers=auth(child_token),
        json={"calleeId": parent["id"]},
    )
    call_id = created.json()["callId"]
    accepted = client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    assert accepted.status_code == 200, accepted.text

    published = client.post(
        "/v1/webhooks/livekit",
        json={
            "event": "track_published",
            "room": {"name": created.json()["roomName"]},
            "participant": {"identity": parent["id"]},
            "track": {"sid": "TR_parent_audio", "type": "AUDIO"},
        },
    )
    assert published.status_code == 204, published.text

    async def get_parent_asset() -> AudioAsset | None:
        database = client.app.state.container.database
        async with database.sessions() as session:
            return await session.scalar(
                select(AudioAsset).where(
                    AudioAsset.call_id == call_id,
                    AudioAsset.kind == AssetKind.WEBRTC_EGRESS_PARENT.value,
                )
            )

    asset = client.portal.call(get_parent_asset)
    assert asset is not None
    assert asset.egress_id is not None


def test_openapi_contains_contract_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    required = {
        "/v1/calls",
        "/v1/calls/{callId}/accept",
        "/v1/calls/{callId}/end",
        "/v1/calls/{callId}/transcript",
        "/v1/calls/{callId}/extraction",
        "/v1/webhooks/livekit",
    }
    assert required.issubset(paths)


def test_team_portal_is_mobile_ready_and_never_exposes_secrets(client: TestClient) -> None:
    page = client.get("/team")
    assert page.status_code == 200
    assert "Collog Team Hub" in page.text
    assert 'name="viewport"' in page.text
    assert "test-secret-with-more-than-thirty-two-characters" not in page.text
    assert page.headers["cache-control"] == "no-store"

    status = client.get("/team/status.json")
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "ok"
    assert payload["providers"]["livekit"]["mode"] == "self-hosted"
    assert payload["providers"]["questionTts"] == {
        "configured": True,
        "provider": "ios-local",
        "mode": "ios-local",
        "language": "ko-KR",
        "model": None,
        "fallback": "ios-local",
        "deepgramKoreanSupported": False,
    }
    assert "apiKey" not in status.text
    assert "secret" not in status.text.lower()
