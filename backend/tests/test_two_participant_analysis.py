from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AcousticFeature,
    AnalysisMode,
    CallParticipant,
    CallRecord,
    CallState,
    HealthExtraction,
    ParticipantStatus,
    RepeatEvent,
)
from tests.conftest import auth
from tests.test_api_flow import onboard_family, tone_wav
from tests.test_family_symmetric import stored_call


def stored_both_call(client: TestClient, parent: dict, child: dict) -> str:
    """두 사람이 모두 분석된 BOTH 통화를 DB 에 바로 넣는다."""
    container = client.app.state.container

    async def insert() -> str:
        ended = datetime.now(UTC)
        async with container.database.sessions() as session:
            call = CallRecord(
                parent_id=parent["id"],
                child_id=child["id"],
                caller_id=child["id"],
                state=CallState.ANALYZED.value,
                room_name="both-room",
                recording_enabled=True,
                started_at=ended - timedelta(minutes=5),
                accepted_at=ended - timedelta(minutes=5),
                ended_at=ended,
                time_slot="MORNING",
                analysis_mode=AnalysisMode.BOTH.value,
                question_target_id=parent["id"],
            )
            session.add(call)
            await session.flush()
            for user_id, label, role, symptom, rate in (
                (parent["id"], "PARENT", "CALLEE", "부모 기침", 100.0),
                (child["id"], "CHILD", "CALLER", "자녀 두통", 200.0),
            ):
                session.add_all([
                    CallParticipant(
                        call_id=call.id, user_id=user_id, role=role, speaker_label=label,
                        analysis_status=ParticipantStatus.ANALYZED.value, speech_sec=30,
                    ),
                    HealthExtraction(
                        call_id=call.id, subject_user_id=user_id, parse_status="OK",
                        symptom=symptom,
                    ),
                    AcousticFeature(
                        call_id=call.id, subject_user_id=user_id, audio_source="DEVICE_RAW",
                        metric="SPEECH_RATE", value=rate, unit="음절/분", status="OK",
                        observed_at=ended,
                    ),
                ])
            session.add(RepeatEvent(
                call_id=call.id, subject_user_id=child["id"], speaker="CHILD", start_ms=0,
                end_ms=500, category="REPEAT_REQUEST", matched_text="뭐라고?",
                rule_id="repeat.what", confidence=0.98, rule_version="repeat-ko-v2",
            ))
            await session.commit()
            return call.id

    return client.portal.call(insert)


def test_both_call_feeds_each_participants_own_report_and_baseline(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    call_id = stored_both_call(client, parent, child)
    client.app.state.container.settings.baseline_required_samples = 1
    expected = {parent["id"]: ("부모 기침", 100.0, 0), child["id"]: ("자녀 두통", 200.0, 1)}
    for subject_id, (symptom, rate, repeats) in expected.items():
        for token in (child_token, parent_token):
            report = client.get(
                f"/v1/parents/{subject_id}/reports?period=WEEKLY", headers=auth(token)
            )
            assert report.status_code == 200, report.text
            body = report.json()
            assert body["analyzedCallCount"] == 1
            assert body["conversationItems"]["symptom"] == [symptom]
            assert body["acousticTrends"][0]["points"][0]["value"] == rate
            assert body["repeatObservation"]["count"] == repeats
        baseline = client.get(
            f"/v1/parents/{subject_id}/baseline?kind=ANCHOR", headers=auth(parent_token)
        )
        assert baseline.status_code == 200
        speech = next(
            item for item in baseline.json()["baselines"]
            if item["metric"] == "SPEECH_RATE" and item["timeSlot"] == "MORNING"
        )
        assert (speech["sampleCount"], speech["median"]) == (1, rate)
        calls = client.get(f"/v1/parents/{subject_id}/calls", headers=auth(child_token))
        assert [item["callId"] for item in calls.json()["calls"]] == [call_id]


def participants_of(client: TestClient, call_id: str) -> list[CallParticipant]:
    async def load() -> list[CallParticipant]:
        async with client.app.state.container.database.sessions() as session:
            return list(await session.scalars(
                select(CallParticipant)
                .where(CallParticipant.call_id == call_id)
                .order_by(CallParticipant.role)
            ))

    return client.portal.call(load)


def upload_url(client: TestClient, call_id: str, token: str):
    return client.post(
        f"/v1/calls/{call_id}/raw-audio/upload-url",
        headers=auth(token),
        json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
    )


def test_both_participants_capture_and_upload_their_own_raw_audio(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    client.app.state.container.settings.two_participant_analysis_enabled = True
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201, created.text
    call_id = created.json()["callId"]
    assert created.json()["rawCaptureRequired"] is True
    call = stored_call(client, call_id)
    assert (call.analysis_mode, call.question_target_id) == ("BOTH", parent["id"])
    assert [
        (row.user_id, row.role, row.speaker_label, row.analysis_status)
        for row in participants_of(client, call_id)
    ] == [
        (parent["id"], "CALLEE", "PARENT", "PENDING"),
        (child["id"], "CALLER", "CHILD", "PENDING"),
    ]
    accepted = client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    assert accepted.json()["rawCaptureRequired"] is True

    child_upload = upload_url(client, call_id, child_token)
    parent_upload = upload_url(client, call_id, parent_token)
    assert child_upload.status_code == parent_upload.status_code == 200
    assert child_upload.json()["assetId"] != parent_upload.json()["assetId"]
    again = upload_url(client, call_id, child_token)
    assert again.json()["assetId"] == child_upload.json()["assetId"]
    for upload in (child_upload, parent_upload):
        parsed = urlsplit(upload.json()["uploadUrl"])
        assert client.put(parsed.path + "?" + parsed.query, content=tone_wav()).status_code == 204
    stolen = client.post(
        f"/v1/calls/{call_id}/raw-audio/complete",
        headers=auth(child_token),
        json={"assetId": parent_upload.json()["assetId"]},
    )
    assert stolen.status_code == 403
    completed = client.post(
        f"/v1/calls/{call_id}/raw-audio/complete",
        headers=auth(child_token),
        json={"assetId": child_upload.json()["assetId"]},
    )
    assert completed.status_code == 202, completed.text


def test_both_call_asks_questions_from_the_callee_profile(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    for subject, conditions in ((parent, ["HYPERTENSION"]), (child, ["ASTHMA"])):
        saved = client.put(
            f"/v1/parents/{subject['id']}/profile",
            headers=auth(child_token),
            json={"conditions": conditions},
        )
        assert saved.status_code == 200, saved.text
    settings = client.app.state.container.settings
    for enabled, prefix, target in ((False, "hypertension-", parent), (True, "asthma-", child)):
        settings.two_participant_analysis_enabled = enabled
        created = client.post(
            "/v1/calls", headers=auth(parent_token), json={"calleeId": child["id"]}
        )
        assert created.status_code == 201, created.text
        call_id = created.json()["callId"]
        assert created.json()["questions"]
        assert all(item["questionId"].startswith(prefix) for item in created.json()["questions"])
        assert stored_call(client, call_id).question_target_id == target["id"]
        ended = client.post(f"/v1/calls/{call_id}/end", headers=auth(parent_token))
        assert ended.status_code == 200


def test_single_call_keeps_raw_audio_with_the_subject_only(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    call_id = created.json()["callId"]
    assert created.json()["rawCaptureRequired"] is False
    call = stored_call(client, call_id)
    assert (call.analysis_mode, call.question_target_id) == ("SINGLE", parent["id"])
    assert participants_of(client, call_id) == []
    client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token))
    assert upload_url(client, call_id, child_token).status_code == 403
    assert upload_url(client, call_id, parent_token).status_code == 200
