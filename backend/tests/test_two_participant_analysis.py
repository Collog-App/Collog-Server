from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

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
from tests.test_api_flow import onboard_family


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
