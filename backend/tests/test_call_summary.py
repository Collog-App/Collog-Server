from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import CallRecord
from app.services.gemini import (
    SUMMARY_MAX_LENGTH,
    GeminiExtractionGateway,
    MockExtractionGateway,
    clean_summary,
)
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_family_symmetric import accept, grant, invite
from tests.test_guardrail import analyze_family_call

LONG_TALK = (
    "PARENT: 어제는 잠을 푹 잤고 아침도 챙겨 먹었어. 오후에는 공원을 삼십 분 정도 "
    "천천히 산책했고 저녁에는 친구랑 통화도 했어. 다른 불편함은 없어."
)


def segments(*lines: tuple[str, str]) -> list[dict]:
    return [
        {"segmentId": f"s{index:04d}", "speaker": speaker, "startMs": index * 1000, "text": text}
        for index, (speaker, text) in enumerate(lines)
    ]


def gemini(monkeypatch: pytest.MonkeyPatch, reply: dict | Exception) -> GeminiExtractionGateway:
    async def fake_request(client: httpx.AsyncClient, method: str, url: str, **kwargs):
        del client, method, url, kwargs
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]}
        )

    monkeypatch.setattr("app.services.gemini.request_with_retry", fake_request)
    return GeminiExtractionGateway(Settings(mock_external_services=False, gemini_api_key="test"))


TALK = segments(("CHILD", "요즘 어떻게 지내세요?"), ("PARENT", "무릎이 좀 아파서 병원에 다녀왔어"))


async def test_gemini_summary_is_returned_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = gemini(monkeypatch, {"summary": "  무릎이 아파 병원에\n다녀온 이야기를 나눔  "})
    assert await gateway.summarize(TALK) == "무릎이 아파 병원에 다녀온 이야기를 나눔"


async def test_gemini_summary_with_an_unsaid_diagnosis_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = gemini(monkeypatch, {"summary": "무릎 통증으로 관절염이 의심됨"})
    assert await gateway.summarize(TALK) is None


async def test_gemini_summary_keeps_a_disease_the_speaker_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    talk = segments(("PARENT", "당뇨 약은 아침마다 잘 챙겨 먹고 있어"))
    gateway = gemini(monkeypatch, {"summary": "당뇨 약을 아침마다 챙겨 먹는다는 이야기를 나눔"})
    assert await gateway.summarize(talk) == "당뇨 약을 아침마다 챙겨 먹는다는 이야기를 나눔"


@pytest.mark.parametrize("reply", [{"summary": ""}, httpx.ConnectError("down"), {"other": 1}])
async def test_gemini_summary_failure_or_empty_reply_gives_none(
    monkeypatch: pytest.MonkeyPatch, reply: dict | Exception
) -> None:
    assert await gemini(monkeypatch, reply).summarize(TALK) is None


async def test_summary_is_not_requested_for_a_nearly_empty_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = gemini(monkeypatch, RuntimeError("must not be called"))
    assert await gateway.summarize(segments(("PARENT", "응"), ("CHILD", "네"))) is None
    assert await MockExtractionGateway().summarize(segments(("PARENT", "응"))) is None


def test_overlong_summary_is_cut_to_the_limit() -> None:
    cleaned = clean_summary("가" * 300, TALK)
    assert cleaned is not None
    assert len(cleaned) == SUMMARY_MAX_LENGTH
    assert cleaned.endswith("…")


def family_of_three(client: TestClient):
    child_token, child, parent_token, parent = onboard_family(client)
    sibling_token, sibling = create_user(client, "01055556666", "CHILD", "둘째")
    family_id = client.get("/v1/families", headers=auth(child_token)).json()["families"][0][
        "familyId"
    ]
    invitation = invite(client, child_token, family_id, "SIBLING")
    assert accept(client, sibling_token, invitation["code"]).status_code == 200
    grant(client, sibling_token)
    return child_token, parent_token, parent, sibling_token, sibling


def test_analyzed_call_keeps_summary_and_timing(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    call_id = analyze_family_call(client, child_token, parent_token, parent, LONG_TALK)

    for token in (child_token, parent_token):
        body = client.get(f"/v1/calls/{call_id}/extraction", headers=auth(token)).json()
        assert body["parseStatus"] == "OK"
        # mock 전사는 화자 접두어를 그대로 둔다.
        assert body["summary"].startswith("PARENT: 어제는 잠을 푹 잤고")

    async def stored() -> CallRecord:
        async with client.app.state.container.database.sessions() as session:
            return await session.get(CallRecord, call_id)

    call = client.portal.call(stored)
    assert set(call.analysis_timing) == {"waitSec", "sttSec", "analysisSec", "totalSec"}
    assert all(value >= 0 for value in call.analysis_timing.values())
    # 분석이 끝나면 주기 작업을 기다리지 않고 바로 알림 처리까지 마친다.
    assert call.report_notified_at is not None


def test_family_member_outside_the_call_cannot_read_its_summary(client: TestClient) -> None:
    # 처리방침: 개별 통화의 전사와 분석 자료는 그 통화에 참여한 계정만 본다.
    child_token, parent_token, parent, sibling_token, _ = family_of_three(client)
    call_id = analyze_family_call(client, child_token, parent_token, parent, LONG_TALK)

    listed = client.get(f"/v1/parents/{parent['id']}/calls", headers=auth(sibling_token))
    assert [item["callId"] for item in listed.json()["calls"]] == [call_id]
    for path in ("extraction", "transcript", "acoustic-features", ""):
        blocked = client.get(
            f"/v1/calls/{call_id}/{path}".rstrip("/"), headers=auth(sibling_token)
        )
        assert blocked.status_code == 403, path


def test_someone_outside_the_family_cannot_read_a_call(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    call_id = analyze_family_call(client, child_token, parent_token, parent, LONG_TALK)
    stranger_token, _ = create_user(client, "01077778888", "CHILD", "남")
    for path in ("extraction", "transcript", "acoustic-features", ""):
        response = client.get(
            f"/v1/calls/{call_id}/{path}".rstrip("/"), headers=auth(stranger_token)
        )
        assert response.status_code == 403, path
