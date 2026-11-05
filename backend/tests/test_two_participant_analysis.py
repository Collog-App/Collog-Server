from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AcousticFeature,
    AnalysisMode,
    AudioAsset,
    Baseline,
    CallParticipant,
    CallRecord,
    CallState,
    ConsentRecord,
    ExtractionEvidence,
    HealthExtraction,
    ParticipantStatus,
    RepeatEvent,
    Report,
)
from app.services.pipeline import segments_for_subject
from scripts.verify_two_iphone_call import evaluate, snapshot
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family, tone_wav
from tests.test_family_symmetric import accept, invite, stored_call


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
                asked_question_ids=["hypertension-med"],
                parent_speech_sec=30,
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


PARENT_SPEECH = (
    "어젯밤에 기침 때문에 두 번 깼어. 그래도 아침 혈압약은 챙겨 먹었고 오후에는 공원을 삼십 분 "
    "정도 천천히 산책했어. 오늘은 잠이 조금 부족해서 평소보다 졸리지만 다른 불편함은 따로 "
    "이야기하지 않았어."
)
CHILD_SPEECH = (
    "저는 요즘 회사 일이 많아서 밤에 잠을 자주 깨요. 어제는 머리가 아팠어요. 그래서 진통제 약을 "
    "먹었고 오늘 아침에는 회사까지 사십 분을 걸어서 출근했어요."
)


def start_call(client: TestClient, both: bool = True):
    """통화를 받고 끊은 뒤 원시 오디오까지 올린다. Egress 는 `deliver_egress` 로 따로 끝낸다."""
    child_token, child, parent_token, parent = onboard_family(client)
    client.app.state.container.settings.two_participant_analysis_enabled = both
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    call_id = created.json()["callId"]
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 200
    assert client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token)).status_code == 200
    for token in (child_token, parent_token) if both else (parent_token,):
        upload = upload_url(client, call_id, token)
        parsed = urlsplit(upload.json()["uploadUrl"])
        assert client.put(parsed.path + "?" + parsed.query, content=tone_wav()).status_code == 204
        completed = client.post(
            f"/v1/calls/{call_id}/raw-audio/complete",
            headers=auth(token),
            json={"assetId": upload.json()["assetId"]},
        )
        assert completed.status_code == 202, completed.text
    return call_id, child_token, child, parent_token, parent


def call_assets(client: TestClient, call_id: str) -> list[AudioAsset]:
    async def load() -> list[AudioAsset]:
        async with client.app.state.container.database.sessions() as session:
            return list(
                await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            )

    return client.portal.call(load)


def deliver_egress(client: TestClient, call_id: str, speech: dict[str, str]) -> None:
    storage = client.app.state.container.storage
    egress = [item for item in call_assets(client, call_id) if item.kind != "DEVICE_RAW"]
    for asset in egress:
        client.portal.call(
            storage.write, storage.object_key(asset.uri), speech[asset.owner_user_id].encode()
        )
    for asset in egress:
        assert asset.egress_id
        webhook = client.post(
            "/v1/webhooks/livekit",
            json={
                "event": "egress_ended",
                "egress_info": {"egress_id": asset.egress_id, "status": "EGRESS_COMPLETE"},
            },
        )
        assert webhook.status_code == 204, webhook.text


def subject_rows(client: TestClient, call_id: str, model: type) -> dict[str, list]:
    async def load() -> dict[str, list]:
        async with client.app.state.container.database.sessions() as session:
            rows: dict[str, list] = {}
            for row in await session.scalars(select(model).where(model.call_id == call_id)):
                rows.setdefault(row.subject_user_id, []).append(row)
            return rows

    return client.portal.call(load)


def test_both_participants_are_analysed_as_their_own_subjects(client: TestClient) -> None:
    call_id, child_token, child, parent_token, parent = start_call(client)
    deliver_egress(client, call_id, {parent["id"]: PARENT_SPEECH, child["id"]: CHILD_SPEECH})

    view = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert view["state"] == "ANALYZED"
    assert view["rawAudioPurgedAt"] is not None
    assert {(row.user_id, row.analysis_status) for row in participants_of(client, call_id)} == {
        (parent["id"], "ANALYZED"), (child["id"], "ANALYZED"),
    }
    transcript = client.get(f"/v1/calls/{call_id}/transcript", headers=auth(child_token)).json()
    speaker_of = {item["segmentId"]: item["speaker"] for item in transcript["segments"]}
    evidence = subject_rows(client, call_id, ExtractionEvidence)
    for subject_id, speaker in ((parent["id"], "PARENT"), (child["id"], "CHILD")):
        facts = evidence[subject_id][0].facts
        assert facts
        assert {
            speaker_of[segment_id] for fact in facts for segment_id in fact["evidenceSegmentIds"]
        } == {speaker}
    features = subject_rows(client, call_id, AcousticFeature)
    assert {key: len(rows) for key, rows in features.items()} == {parent["id"]: 4, child["id"]: 4}
    speech = {item["userId"]: item["speechSec"] for item in view["participants"]}
    for subject_id, word in ((parent["id"], "기침"), (child["id"], "머리")):
        report = client.get(
            f"/v1/parents/{subject_id}/reports?period=WEEKLY", headers=auth(parent_token)
        ).json()
        assert report["analyzedCallCount"] == 1
        assert [word in item for item in report["conversationItems"]["symptom"]] == [True]
        query = f"?subjectId={subject_id}"
        extraction = client.get(f"/v1/calls/{call_id}/extraction{query}", headers=auth(child_token))
        assert word in extraction.json()["symptom"]
        transcript = client.get(f"/v1/calls/{call_id}/transcript{query}", headers=auth(child_token))
        assert transcript.json()["parentSpeechSec"] == speech[subject_id]
        acoustics = client.get(
            f"/v1/calls/{call_id}/acoustic-features{query}", headers=auth(child_token)
        )
        assert len(acoustics.json()["features"]) == 4
    default = client.get(f"/v1/calls/{call_id}/extraction", headers=auth(child_token)).json()
    assert "기침" in default["symptom"]


def test_short_speech_excludes_only_that_participant(client: TestClient) -> None:
    call_id, child_token, child, _, parent = start_call(client)
    extract = AsyncMock(wraps=client.app.state.container.pipeline.extraction.extract)
    client.app.state.container.pipeline.extraction.extract = extract
    deliver_egress(client, call_id, {parent["id"]: PARENT_SPEECH, child["id"]: "네 저도 잘 지내요"})

    view = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert view["state"] == "ANALYZED"
    assert view["rawAudioPurgedAt"] is not None
    assert {
        (row.user_id, row.analysis_status, row.exclusion_reason)
        for row in participants_of(client, call_id)
    } == {(parent["id"], "ANALYZED", None), (child["id"], "EXCLUDED", "INSUFFICIENT_SPEECH")}
    assert extract.await_count == 1
    assert set(subject_rows(client, call_id, HealthExtraction)) == {parent["id"]}
    child_report = client.get(
        f"/v1/parents/{child['id']}/reports?period=WEEKLY", headers=auth(child_token)
    ).json()
    assert child_report["analyzedCallCount"] == 0
    assert child_report["state"] != "EMPTY"


def test_transient_error_keeps_audio_and_retries_without_billing_again(
    client: TestClient,
) -> None:
    call_id, child_token, child, _, parent = start_call(client)
    container = client.app.state.container
    pipeline = container.pipeline
    transcribe = AsyncMock(wraps=pipeline.stt.transcribe)
    extract = AsyncMock(wraps=pipeline.extraction.extract)
    pipeline.stt.transcribe = transcribe
    pipeline.extraction.extract = extract
    parent_raw = next(
        item.uri for item in call_assets(client, call_id)
        if item.kind == "DEVICE_RAW" and item.owner_user_id == parent["id"]
    )
    original_read = pipeline.storage.read
    failed: list[str] = []

    async def flaky_read(uri: str) -> bytes:
        if uri == parent_raw and not failed:
            failed.append(uri)
            raise TimeoutError()
        return await original_read(uri)

    pipeline.storage.read = flaky_read
    deliver_egress(client, call_id, {parent["id"]: PARENT_SPEECH, child["id"]: CHILD_SPEECH})

    call = stored_call(client, call_id)
    assert call.state == CallState.ENDED.value
    assert call.processing_retry_at is not None
    assert call.raw_audio_purged_at is None
    assert client.portal.call(container.storage.exists, parent_raw)
    assert {(row.user_id, row.analysis_status) for row in participants_of(client, call_id)} == {
        (parent["id"], "PENDING"), (child["id"], "ANALYZED"),
    }

    async def release() -> None:
        async with container.database.sessions() as session:
            row = await session.get(CallRecord, call_id)
            row.processing_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    client.portal.call(release)
    client.portal.call(pipeline.process, call_id)

    view = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert view["state"] == "ANALYZED"
    assert view["rawAudioPurgedAt"] is not None
    assert not client.portal.call(container.storage.exists, parent_raw)
    assert (transcribe.await_count, extract.await_count) == (2, 2)
    assert {row.analysis_status for row in participants_of(client, call_id)} == {"ANALYZED"}


def test_failed_second_track_does_not_resend_the_first_to_stt(client: TestClient) -> None:
    call_id, child_token, child, _, parent = start_call(client)
    container = client.app.state.container
    pipeline = container.pipeline
    original = pipeline.stt.transcribe
    speakers: list[str] = []

    async def flaky_transcribe(audio: bytes, content_type: str, speaker: str):
        speakers.append(speaker)
        if speakers == ["PARENT", "CHILD"]:
            raise TimeoutError()
        return await original(audio, content_type, speaker)

    pipeline.stt.transcribe = flaky_transcribe
    deliver_egress(client, call_id, {parent["id"]: PARENT_SPEECH, child["id"]: CHILD_SPEECH})

    assert stored_call(client, call_id).state == CallState.ENDED.value
    kept = {row.speaker_label: row.stt_result for row in participants_of(client, call_id)}
    assert kept["PARENT"]["segments"] and kept["CHILD"] is None

    async def release() -> None:
        async with container.database.sessions() as session:
            row = await session.get(CallRecord, call_id)
            row.processing_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    client.portal.call(release)
    client.portal.call(pipeline.process, call_id)

    assert speakers == ["PARENT", "CHILD", "CHILD"]
    assert stored_call(client, call_id).state == CallState.ANALYZED.value
    assert {row.stt_result for row in participants_of(client, call_id)} == {None}
    transcript = client.get(f"/v1/calls/{call_id}/transcript", headers=auth(child_token)).json()
    assert {item["speaker"] for item in transcript["segments"]} == {"PARENT", "CHILD"}


def test_exhausted_retry_closes_the_rest_and_purges(client: TestClient) -> None:
    call_id, child_token, child, _, parent = start_call(client)
    container = client.app.state.container
    pipeline = container.pipeline
    parent_raw = next(
        item.uri for item in call_assets(client, call_id)
        if item.kind == "DEVICE_RAW" and item.owner_user_id == parent["id"]
    )
    original_read = pipeline.storage.read

    async def broken_read(uri: str) -> bytes:
        if uri == parent_raw:
            raise TimeoutError()
        return await original_read(uri)

    async def last_attempt() -> None:
        async with container.database.sessions() as session:
            row = await session.get(CallRecord, call_id)
            row.processing_attempts = 2
            await session.commit()

    client.portal.call(last_attempt)
    pipeline.storage.read = broken_read
    deliver_egress(client, call_id, {parent["id"]: PARENT_SPEECH, child["id"]: CHILD_SPEECH})

    view = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert view["state"] == "ANALYZED"
    assert view["rawAudioPurgedAt"] is not None
    assert {
        (row.user_id, row.analysis_status, row.exclusion_reason)
        for row in participants_of(client, call_id)
    } == {(parent["id"], "FAILED", "ANALYSIS_ERROR"), (child["id"], "ANALYZED", None)}


def test_withdrawal_after_one_analysis_keeps_the_call_analysed(client: TestClient) -> None:
    call_id, child_token, child, _, parent = start_call(client)
    container = client.app.state.container
    pipeline = container.pipeline
    child_raw = next(
        item.uri for item in call_assets(client, call_id)
        if item.kind == "DEVICE_RAW" and item.owner_user_id == child["id"]
    )
    original_read = pipeline.storage.read

    async def withdraw_then_read(uri: str) -> bytes:
        if uri == child_raw:
            async with container.database.sessions() as session:
                session.add(ConsentRecord(
                    user_id=child["id"],
                    document_version=container.settings.consent_document_version,
                    decision="DENIED",
                    agreed_items=[],
                    agreed_at=datetime.now(UTC),
                ))
                await session.commit()
        return await original_read(uri)

    pipeline.storage.read = withdraw_then_read
    deliver_egress(client, call_id, {parent["id"]: PARENT_SPEECH, child["id"]: CHILD_SPEECH})

    view = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert view["state"] == "ANALYZED"
    assert view["rawAudioPurgedAt"] is not None
    assert {
        (row.user_id, row.analysis_status, row.exclusion_reason)
        for row in participants_of(client, call_id)
    } == {(parent["id"], "ANALYZED", None), (child["id"], "EXCLUDED", "ANALYSIS_NOT_ALLOWED")}


def test_calls_ending_without_analysis_close_their_participants(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    container = client.app.state.container
    container.settings.two_participant_analysis_enabled = True

    def ring() -> str:
        created = client.post(
            "/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]}
        )
        assert created.status_code == 201, created.text
        return created.json()["callId"]

    declined = ring()
    decline = client.post(f"/v1/calls/{declined}/decline", headers=auth(parent_token))
    assert decline.status_code == 200
    missed = ring()
    assert client.post(f"/v1/calls/{missed}/end", headers=auth(child_token)).status_code == 200
    document = client.get("/v1/consents/document").json()
    denied = client.post(
        "/v1/consents",
        headers=auth(child_token),
        json={
            "documentVersion": document["version"],
            "decision": "DENY",
            "scrolledToEnd": False,
            "agreedItems": [],
        },
    )
    assert denied.status_code == 201, denied.text
    unrecorded = ring()
    accept_call = client.post(f"/v1/calls/{unrecorded}/accept", headers=auth(parent_token))
    assert accept_call.status_code == 200
    assert client.post(f"/v1/calls/{unrecorded}/end", headers=auth(child_token)).status_code == 200
    # 끝난 뒤에 녹음이 꺼진 통화는 주기 정리(maintain)가 닫는다.
    stopped = ring()

    async def end_unrecorded() -> None:
        async with container.database.sessions() as session:
            row = await session.get(CallRecord, stopped)
            row.state = CallState.ENDED.value
            row.ended_at = datetime.now(UTC)
            row.recording_enabled = False
            row.recording_disabled_reason = None
            await session.commit()

    client.portal.call(end_unrecorded)
    client.portal.call(container.calls.maintain)

    for call_id, reason in (
        (declined, "CALL_NOT_ANSWERED"),
        (missed, "CALL_NOT_ANSWERED"),
        (unrecorded, "CONSENT_DENIED"),
        (stopped, "RECORDING_DISABLED"),
    ):
        view = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
        assert view["state"] == "ANALYSIS_EXCLUDED"
        assert {
            (item["analysisStatus"], item["exclusionReason"]) for item in view["participants"]
        } == {("EXCLUDED", reason)}


def test_retention_limit_closes_participants_only_of_calls_it_fails(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    container = client.app.state.container
    container.settings.two_participant_analysis_enabled = True
    call_ids = []
    for hang_up in (True, False):
        created = client.post(
            "/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]}
        )
        call_ids.append(created.json()["callId"])
        accepted = client.post(f"/v1/calls/{call_ids[-1]}/accept", headers=auth(parent_token))
        assert accepted.status_code == 200
        if hang_up:
            assert client.post(
                f"/v1/calls/{call_ids[-1]}/end", headers=auth(child_token)
            ).status_code == 200
    ended, live = call_ids

    async def age_audio() -> None:
        async with container.database.sessions() as session:
            for asset in await session.scalars(select(AudioAsset)):
                asset.created_at = datetime.now(UTC) - timedelta(hours=25)
            await session.commit()

    client.portal.call(age_audio)
    client.portal.call(container.pipeline.purge_expired_audio)

    assert stored_call(client, ended).state == CallState.ANALYSIS_FAILED.value
    assert {
        (row.analysis_status, row.exclusion_reason) for row in participants_of(client, ended)
    } == {("FAILED", "AUDIO_RETENTION_LIMIT")}
    # 아직 통화 중인 통화는 상태가 그대로라 참여자도 그대로다.
    assert stored_call(client, live).state == CallState.ACTIVE.value
    assert {row.analysis_status for row in participants_of(client, live)} == {"PENDING"}


def test_call_created_while_off_stays_single_after_turning_on(client: TestClient) -> None:
    call_id, child_token, child, _, parent = start_call(client, both=False)
    client.app.state.container.settings.two_participant_analysis_enabled = True
    deliver_egress(client, call_id, {parent["id"]: PARENT_SPEECH, child["id"]: CHILD_SPEECH})

    call = stored_call(client, call_id)
    assert (call.state, call.analysis_mode) == ("ANALYZED", "SINGLE")
    assert participants_of(client, call_id) == []
    assert set(subject_rows(client, call_id, HealthExtraction)) == {parent["id"]}
    assert set(subject_rows(client, call_id, AcousticFeature)) == {parent["id"]}


def test_call_views_show_participants_only_for_both_calls(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    single = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    single_id = single.json()["callId"]
    client.post(f"/v1/calls/{single_id}/end", headers=auth(child_token))
    both_id = stored_both_call(client, parent, child)

    single_view = client.get(f"/v1/calls/{single_id}", headers=auth(child_token)).json()
    assert "analysisMode" not in single_view and "participants" not in single_view
    both_view = client.get(f"/v1/calls/{both_id}", headers=auth(child_token)).json()
    assert both_view["analysisMode"] == "BOTH"
    assert [(item["userId"], item["role"], item["analysisStatus"]) for item in both_view[
        "participants"
    ]] == [(child["id"], "CALLER", "ANALYZED"), (parent["id"], "CALLEE", "ANALYZED")]
    listed = client.get(f"/v1/parents/{parent['id']}/calls", headers=auth(parent_token)).json()
    by_id = {item["callId"]: item for item in listed["calls"]}
    assert len(by_id[both_id]["participants"]) == 2
    assert "participants" not in by_id[single_id]

    # SINGLE 통화에는 자녀 몫의 결과가 없다.
    assert client.get(
        f"/v1/calls/{single_id}/extraction?subjectId={child['id']}", headers=auth(child_token)
    ).status_code == 404
    extraction = client.get(
        f"/v1/calls/{both_id}/extraction?subjectId={child['id']}", headers=auth(parent_token)
    )
    assert extraction.json()["symptom"] == "자녀 두통"
    shared = client.put(
        "/v1/account/sharing",
        headers=auth(child_token),
        json={"shares": [{"viewerUserId": parent["id"], "report": False, "timeline": True}]},
    )
    assert shared.status_code == 200, shared.text
    assert client.get(
        f"/v1/calls/{both_id}/extraction?subjectId={child['id']}", headers=auth(parent_token)
    ).status_code == 403
    assert client.get(
        f"/v1/calls/{both_id}/extraction?subjectId={child['id']}", headers=auth(child_token)
    ).status_code == 200


def hide(client: TestClient, owner_token: str, viewer_id: str, **scopes: bool) -> None:
    shares = {"report": True, "timeline": True, **scopes}
    response = client.put(
        "/v1/account/sharing",
        headers=auth(owner_token),
        json={"shares": [{"viewerUserId": viewer_id, **shares}]},
    )
    assert response.status_code == 200, response.text


def test_default_subject_of_a_both_call_respects_report_sharing(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    both_id = stored_both_call(client, parent, child)
    single = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    single_id = single.json()["callId"]
    hide(client, parent_token, child["id"], report=False)

    for path in ("extraction", "acoustic-features"):
        assert client.get(
            f"/v1/calls/{both_id}/{path}", headers=auth(child_token)
        ).status_code == 403
        assert client.get(
            f"/v1/calls/{both_id}/{path}?subjectId={child['id']}", headers=auth(child_token)
        ).status_code == 200
        assert client.get(
            f"/v1/calls/{both_id}/{path}", headers=auth(parent_token)
        ).status_code == 200
    # SINGLE 통화는 예전처럼 통화 참여자 확인만 한다(403 이 아니라 결과가 아직 없어 404).
    single_extraction = client.get(f"/v1/calls/{single_id}/extraction", headers=auth(child_token))
    assert single_extraction.status_code == 404


def test_call_list_hides_a_participant_who_blocks_the_viewers_timeline(
    client: TestClient,
) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    third_token, third = create_user(client, "01099990001", "PARENT", "셋째")
    joined = accept(client, third_token, invite(client, child_token, child["familyId"])["code"])
    assert joined.status_code == 200, joined.text
    call_id = stored_both_call(client, parent, child)

    def seen_by_third() -> dict:
        calls = client.get(f"/v1/parents/{child['id']}/calls", headers=auth(third_token)).json()
        return next(item for item in calls["calls"] if item["callId"] == call_id)

    shown = seen_by_third()
    assert (shown["parentSpeechSec"], shown["askedQuestionIds"]) == (30, ["hypertension-med"])
    assert {item["userId"] for item in shown["participants"]} == {parent["id"], child["id"]}
    hide(client, parent_token, third["id"], timeline=False)
    hidden = seen_by_third()
    assert (hidden["parentSpeechSec"], hidden["askedQuestionIds"]) == (None, [])
    assert [item["userId"] for item in hidden["participants"]] == [child["id"]]
    # 통화 당사자는 그대로 본다.
    own = client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()
    assert (own["parentSpeechSec"], len(own["participants"])) == (30, 2)


def test_deleting_the_subject_also_removes_the_other_sides_reports(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    stored_both_call(client, parent, child)
    for subject_id in (parent["id"], child["id"]):
        client.get(f"/v1/parents/{subject_id}/reports?period=WEEKLY", headers=auth(child_token))
        client.get(f"/v1/parents/{subject_id}/baseline", headers=auth(child_token))
    assert client.delete("/v1/account", headers=auth(parent_token)).status_code == 204

    async def leftovers() -> tuple[int, int, int]:
        async with client.app.state.container.database.sessions() as session:
            counts = []
            for model, column in (
                (CallParticipant, CallParticipant.user_id),
                (Report, Report.parent_id),
                (Baseline, Baseline.parent_id),
            ):
                counts.append(len(list(await session.scalars(
                    select(model).where(column == child["id"])
                ))))
            return tuple(counts)

    assert client.portal.call(leftovers) == (0, 0, 0)


def test_two_iphone_verifier_reads_the_parents_share_of_a_both_call(client: TestClient) -> None:
    _, child, _, parent = onboard_family(client)
    call_id = stored_both_call(client, parent, child)
    database = client.app.state.container.database

    async def add_raw_audio() -> None:
        async with database.sessions() as session:
            for owner in (parent["id"], child["id"]):
                session.add(AudioAsset(
                    call_id=call_id, kind="DEVICE_RAW", uri=f"memory://{owner}.wav",
                    content_type="audio/wav", owner_user_id=owner, status="PURGED",
                ))
            await session.commit()

    client.portal.call(add_raw_audio)
    data = client.portal.call(snapshot, database, call_id)
    assert data["extraction"]["symptom"] == "부모 기침"
    assert [item["value"] for item in data["features"]] == [100.0]
    checks = {item["name"]: item for item in evaluate(data, 0, False)["checks"]}
    assert checks["parent device raw PCM"]["actual"]["ownerUserId"] == parent["id"]


def test_segments_for_subject_relabels_without_moving_segment_ids() -> None:
    segments = [
        {"segmentId": "s0000", "speaker": "PARENT", "text": "괜찮아"},
        {"segmentId": "s0001", "speaker": "CHILD", "text": "머리가 아팠어요"},
    ]
    relabelled = segments_for_subject(segments, "CHILD")
    assert [(item["segmentId"], item["speaker"]) for item in relabelled] == [
        ("s0000", "CHILD"), ("s0001", "PARENT"),
    ]
    assert segments_for_subject(segments, "PARENT") == segments
    assert segments[0]["speaker"] == "PARENT"


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
