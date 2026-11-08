from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.models import AssetKind, AudioAsset, CallRecord, CallState, HealthExtraction, Report, User
from app.schemas import ExtractionFact, GeminiExtractionResponse
from app.services.gemini import GeminiExtractionGateway, validate_facts
from app.services.guardrail import (
    REVIEW_INSTRUCTION,
    SAFETY_NOTICE,
    adds_medical_claim,
    review_facts,
    safety_notice,
)
from app.services.reports import ReportService, aware_datetime, period_bounds
from tests.conftest import auth
from tests.test_api_flow import onboard_family, tone_wav


def parent_says(text: str) -> list[dict]:
    return [{"id": "p1", "speaker": "PARENT", "text": text}]


def one_fact(category: str, summary: str) -> GeminiExtractionResponse:
    return GeminiExtractionResponse(facts=[ExtractionFact(
        category=category, summary=summary, polarity="PRESENT", evidence_segment_ids=["p1"],
    )])


@pytest.mark.parametrize(
    "said", ["가슴이 너무 아파", "머리가 아파", "허리가 아팠어", "배가 아픈 것 같아", "다리가 아퍼"]
)
def test_symptom_grounding_accepts_common_forms_of_apeuda(said: str) -> None:
    # 「아프」만 보던 근거 규칙이 「아파·아팠·아픈·아퍼」로 말한 증상을 버리던 문제
    # (시나리오 chest-pain-emergency).
    result = validate_facts(one_fact("symptom", said), parent_says(said))
    assert [fact.summary for fact in result.facts] == [said]


@pytest.mark.parametrize(
    ("category", "summary", "said"),
    [
        ("medication", "당뇨약을 먹었음", "당뇨약은 아침에 먹었어"),
        ("symptom", "비염이라 코가 불편하다고 함", "병원에서 비염이래, 코가 불편해"),
        ("medication", "의사가 약을 줄이자고 해서 줄였음", "의사가 약을 줄이자고 해서 줄였어"),
        # 부모가 직접 말한 복약 변화(서술형)는 어간이 근거에 있으면 남긴다.
        ("medication", "혈압약을 줄이셨다", "혈압약을 좀 줄였어"),
        ("medication", "약은 최근 줄이는 중", "약은 요즘 줄이고 있어"),
        ("medication", "새 처방약을 받아옴", "병원에서 약 새로 받아왔어"),
        # 부모가 말한 위험 표현은 옮겨도 된다.
        (
            "symptom",
            "의사가 넘어질 위험이 있다고 했다고 함",
            "의사가 넘어질 위험이 있대, 다리가 아프대",
        ),
        # 「암만」(아무리)은 병명이 아니다.
        ("activity", "암만 걸어도 괜찮다고 함", "아무리 걸어도 괜찮아"),
    ],
)
def test_rule_check_keeps_what_the_parent_said(category: str, summary: str, said: str) -> None:
    assert not adds_medical_claim(summary, said)
    result = validate_facts(one_fact(category, summary), parent_says(said))
    assert [fact.summary for fact in result.facts] == [summary]


@pytest.mark.parametrize(
    ("category", "summary", "said"),
    [
        ("symptom", "기침이 있어 폐렴이 의심됨", "어제부터 기침이 나"),
        ("sleep", "잠을 자주 깨서 우울증 가능성이 있음", "요즘 잠을 자주 깨"),
        ("symptom", "숨이 차서 심부전 위험이 있음", "계단 오르면 숨이 차"),
        ("medication", "혈압약을 꾸준히 복용하세요", "혈압약 먹었어"),
        ("medication", "약을 줄이는 게 좋겠음", "약 먹는 게 귀찮아"),
        ("symptom", "어지러움은 빈혈 증상이다", "아침에 좀 어지러웠어"),
        # 지시형은 부모가 비슷한 말을 했어도 버린다.
        ("medication", "혈압약을 줄이세요", "혈압약을 좀 줄였어"),
        ("medication", "혈압약을 바꿔야 함", "혈압약을 바꿨어"),
        # 서술형이라도 근거에 같은 어간이 없으면 버린다.
        ("medication", "혈압약을 줄이셨다", "혈압약 먹었어"),
        ("medication", "새 처방약을 받아옴", "약 먹었어"),
        # 위험·가능성 평가
        ("symptom", "다리가 불편해 넘어질 위험도 높음", "다리가 좀 불편해"),
        ("sleep", "잠을 못 자 수면장애 가능성 있음", "요즘 잠을 못 자"),
        # 병명 사전
        ("symptom", "무릎이 불편해 관절염이 있음", "무릎이 불편해"),
        ("medication", "고지혈증 약을 먹었음", "콜레스테롤 약 먹었어"),
        ("symptom", "골다공증 때문에 허리가 불편함", "허리가 불편해"),
        ("symptom", "허리가 불편해 디스크로 보임", "허리가 불편해"),
        ("symptom", "목이 아파 암이 걱정됨", "목이 아파"),
        ("symptom", "기침이 계속되어 폐암이 걱정됨", "기침이 계속 나"),
    ],
)
def test_rule_check_drops_added_medical_claims(category: str, summary: str, said: str) -> None:
    assert adds_medical_claim(summary, said)
    assert validate_facts(one_fact(category, summary), parent_says(said)).facts == []


@pytest.mark.parametrize(
    "text",
    [
        "어제 저녁에 가슴이 답답해서 한참 앉아 있었어",
        "가슴이 아파",
        "화장실에서 쓰러졌어",
        "숨을 못 쉬겠더라",
        "아침부터 말이 꼬여",
        "한쪽이 저려서 숟가락을 놓쳤어",
        # 조사가 없거나 사이에 말이 끼는 흔한 말투
        "가슴 아파",
        "가슴이 너무 아파",
        "가슴이 좀 답답해",
        "숨을 잘 못 쉬겠어",
        "숨이 안 쉬어져",
        "숨 막혀",
        "숨쉬기 힘들어",
        "말이 좀 어눌해",
        "한쪽 팔이 저려",
        "정신을 잃었어",
        "피 토했어",
        # 가만히 있어도 숨이 찬 것은 잡는다.
        "가만히 있어도 숨이 차",
        "누워 있어도 숨이 차",
        "쉬어도 숨이 차",
        "앉아 있어도 숨이 차",
    ],
)
def test_safety_notice_finds_emergency_words_in_parent_speech(text: str) -> None:
    segments = [{"speaker": "CHILD", "text": "잘 지내셨어요?"}, {"speaker": "PARENT", "text": text}]
    assert safety_notice(segments) == SAFETY_NOTICE


@pytest.mark.parametrize(
    "text",
    [
        # 부정
        "가슴이 아프지 않아",
        "가슴이 안 아파",
        "가슴이 답답하진 않아",
        "쓰러지진 않았어",
        # 물건이 쓰러진 것, 관용 표현
        "컵을 쓰러뜨렸어",
        "기절할 뻔했어",
        "기절하는 줄 알았어",
        "피곤해서 토했어",
        # 움직인 뒤 숨이 차는 것은 흔해서 띄우지 않는다(2026-11-08 결정).
        "숨이 좀 차",
        "운동하고 나니 숨이 차더라",
        "가만히 있으면 숨이 안 차",
    ],
)
def test_safety_notice_skips_negations_and_idioms(text: str) -> None:
    assert safety_notice([{"speaker": "PARENT", "text": text}]) is None


def test_eval_dyspnea_case_is_not_an_emergency() -> None:
    # evals의 「계단을 오르니 숨이 찼어」는 증상으로 기록하되 응급 안내는 띄우지 않는다.
    cases = json.loads((Path(__file__).parents[1] / "evals/extraction_cases.json").read_text())
    [case] = [item for item in cases if item["id"] == "dyspnea"]
    assert case["segments"][0]["text"] == "계단을 오르니 숨이 찼어"
    assert safety_notice(case["segments"]) is None


def test_safety_notice_ignores_child_speech_and_ordinary_talk() -> None:
    assert safety_notice([
        {"speaker": "CHILD", "text": "쓰러지신 적 있어요? 가슴이 답답하진 않으세요?"},
        {"speaker": "PARENT", "text": "아니 괜찮아, 오늘 공원 산책했어"},
    ]) is None


def facts() -> list[ExtractionFact]:
    return [
        ExtractionFact(
            category="symptom", summary="기침함", polarity="PRESENT",
            evidence_segment_ids=["s0000"],
        ),
        ExtractionFact(
            category="activity", summary="산책함", polarity="PRESENT",
            evidence_segment_ids=["s0001"],
        ),
    ]


SEGMENTS = [
    {"id": "s0000", "speaker": "PARENT", "text": "기침했어"},
    {"id": "s0001", "speaker": "PARENT", "text": "공원 산책했어"},
    {"id": "s0002", "speaker": "CHILD", "text": "약은 드셨어요?"},
]


async def test_llm_review_drops_flagged_facts() -> None:
    captured: dict = {}

    async def generate(instruction: str, text: str, schema: dict) -> str:
        captured.update(instruction=instruction, payload=json.loads(text), schema=schema)
        return json.dumps({"violations": [{"index": 1, "reason": "진단을 덧붙임"}]})

    kept = await review_facts(facts(), SEGMENTS, generate)

    assert [fact.summary for fact in kept] == ["기침함"]
    assert captured["instruction"] == REVIEW_INSTRUCTION
    assert "violations" in captured["schema"]["properties"]
    assert [item["index"] for item in captured["payload"]["facts"]] == [0, 1]
    # 근거가 된 PARENT 발화만 보낸다.
    assert captured["payload"]["segments"] == [
        {"id": "s0000", "text": "기침했어"},
        {"id": "s0001", "text": "공원 산책했어"},
    ]


@pytest.mark.parametrize("failure", ["error", "malformed"])
async def test_llm_review_failure_keeps_rule_checked_facts(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def generate(instruction: str, text: str, schema: dict) -> str:
        del instruction, text, schema
        if failure == "error":
            raise httpx.ConnectError("unreachable")
        return '{"facts": []}'

    with caplog.at_level(logging.WARNING, logger="app.services.guardrail"):
        kept = await review_facts(facts(), SEGMENTS, generate)

    assert kept == facts()
    assert "guardrail LLM review failed" in caplog.text


def gemini_reply(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{
        "finishReason": "STOP",
        "content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]},
    }]})


EXTRACTED = {"facts": [
    {"category": "symptom", "summary": "기침함", "polarity": "PRESENT",
     "evidenceSegmentIds": ["s0000"]},
    {"category": "activity", "summary": "산책함", "polarity": "PRESENT",
     "evidenceSegmentIds": ["s0001"]},
]}


@pytest.mark.parametrize(
    ("settings", "review", "expected_calls", "expected"),
    [
        ({}, {"violations": [{"index": 1, "reason": "x"}]}, 2, ["기침함"]),
        ({}, httpx.ConnectError("unreachable"), 2, ["기침함", "산책함"]),
        ({"guardrail_llm_review": False}, None, 1, ["기침함", "산책함"]),
        ({"mock_external_services": True}, None, 1, ["기침함", "산책함"]),
    ],
)
async def test_gateway_runs_llm_review_after_rules(
    monkeypatch: pytest.MonkeyPatch, settings: dict, review, expected_calls: int, expected: list
) -> None:
    bodies: list[dict] = []

    async def fake_request(client: httpx.AsyncClient, method: str, url: str, **kwargs):
        del client, method, url
        bodies.append(kwargs["json"])
        if len(bodies) == 1:
            return gemini_reply(EXTRACTED)
        if isinstance(review, Exception):
            raise review
        return gemini_reply(review)

    monkeypatch.setattr("app.services.gemini.request_with_retry", fake_request)
    gateway = GeminiExtractionGateway(
        Settings(**{"mock_external_services": False, "gemini_api_key": "test", **settings})
    )

    result = await gateway.extract("PARENT: 기침했어\nPARENT: 공원 산책했어")

    assert [fact.summary for fact in result.facts] == expected
    assert len(bodies) == expected_calls
    if expected_calls == 2:
        review_body = bodies[1]
        assert review_body["systemInstruction"]["parts"][0]["text"] == REVIEW_INSTRUCTION
        assert "violations" in review_body["generationConfig"]["responseJsonSchema"]["properties"]


def analyze_call(
    client: TestClient, parent_text: str, state: str = "ANALYZED"
) -> tuple[str, str, str]:
    """mock 외부 서비스로 통화 한 건을 끝까지 분석한다(test_complete_call_pipeline 흐름)."""
    child_token, _, parent_token, parent = onboard_family(client)
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201, created.text
    call_id = created.json()["callId"]
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 200
    assert client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token)).status_code == 200
    upload = client.post(
        f"/v1/calls/{call_id}/raw-audio/upload-url",
        headers=auth(parent_token),
        json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
    )
    parsed = urlsplit(upload.json()["uploadUrl"])
    assert client.put(parsed.path + "?" + parsed.query, content=tone_wav()).status_code == 204
    completed = client.post(
        f"/v1/calls/{call_id}/raw-audio/complete",
        headers=auth(parent_token),
        json={"assetId": upload.json()["assetId"]},
    )
    assert completed.status_code == 202

    async def prepare_egress_assets() -> list[AudioAsset]:
        async with client.app.state.container.database.sessions() as session:
            assets = list(
                await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
            )
        storage = client.app.state.container.storage
        for asset in assets:
            text = {
                AssetKind.WEBRTC_EGRESS_PARENT.value: parent_text,
                AssetKind.WEBRTC_EGRESS_CHILD.value: "CHILD: 쓰러지신 적은 없으시죠?",
            }.get(asset.kind)
            if text:
                await storage.write(asset.uri.removeprefix("local://"), text.encode())
        return [asset for asset in assets if asset.egress_id]

    for asset in client.portal.call(prepare_egress_assets):
        webhook = client.post(
            "/v1/webhooks/livekit",
            json={
                "event": "egress_ended",
                "egress_info": {"egress_id": asset.egress_id, "status": "EGRESS_COMPLETE"},
            },
        )
        assert webhook.status_code == 204, webhook.text
    assert client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()["state"] == state
    return call_id, child_token, parent["id"]


def test_pipeline_saves_safety_notice_and_reports_it(client: TestClient) -> None:
    call_id, child_token, parent_id = analyze_call(
        client,
        "PARENT: 어제 저녁에 가슴이 답답해서 한참 앉아 있었어. 지금은 좀 나아졌고 아침도 "
        "챙겨 먹었어. 오후에는 공원을 삼십 분 정도 천천히 산책했고 잠도 평소처럼 잤어.",
    )

    extraction = client.get(f"/v1/calls/{call_id}/extraction", headers=auth(child_token))
    assert extraction.status_code == 200, extraction.text
    assert extraction.json()["safetyNotice"] == SAFETY_NOTICE

    report = client.get(f"/v1/parents/{parent_id}/reports?period=WEEKLY", headers=auth(child_token))
    assert report.status_code == 200, report.text
    [notice] = report.json()["safetyNotices"]
    assert notice["callId"] == call_id
    assert notice["text"] == SAFETY_NOTICE
    assert datetime.fromisoformat(notice["observedAt"]).tzinfo is not None


def test_short_excluded_call_still_keeps_safety_notice(client: TestClient) -> None:
    # 부모 발화가 기준(parent_min_speech_seconds)보다 짧아 분석에서 빠지는 통화
    call_id, child_token, parent_id = analyze_call(
        client, "PARENT: 가슴이 아파", state="ANALYSIS_EXCLUDED"
    )

    extraction = client.get(f"/v1/calls/{call_id}/extraction", headers=auth(child_token))
    assert extraction.status_code == 200, extraction.text
    body = extraction.json()
    assert body["safetyNotice"] == SAFETY_NOTICE
    assert body["parseStatus"] == "EXCLUDED"
    assert body["facts"] == []

    report = client.get(f"/v1/parents/{parent_id}/reports?period=WEEKLY", headers=auth(child_token))
    assert report.status_code == 200, report.text
    assert report.json()["analyzedCallCount"] == 0
    assert [item["callId"] for item in report.json()["safetyNotices"]] == [call_id]


def test_short_excluded_call_without_emergency_has_no_extraction(client: TestClient) -> None:
    call_id, child_token, _ = analyze_call(
        client, "PARENT: 응 잘 지내", state="ANALYSIS_EXCLUDED"
    )
    extraction = client.get(f"/v1/calls/{call_id}/extraction", headers=auth(child_token))
    assert extraction.status_code == 404


def test_pipeline_leaves_safety_notice_empty_for_ordinary_call(client: TestClient) -> None:
    call_id, child_token, parent_id = analyze_call(
        client,
        "PARENT: 어제는 잠을 푹 잤고 아침도 챙겨 먹었어. 오후에는 공원을 삼십 분 정도 "
        "천천히 산책했고 저녁에는 친구랑 통화도 했어. 다른 불편함은 없어.",
    )
    extraction = client.get(f"/v1/calls/{call_id}/extraction", headers=auth(child_token))
    assert extraction.json()["safetyNotice"] is None
    report = client.get(f"/v1/parents/{parent_id}/reports?period=WEEKLY", headers=auth(child_token))
    assert report.json()["safetyNotices"] == []


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notices.db'}")
    await database.ensure_schema(auto_reset=True)
    yield database
    await database.close()


async def test_report_lists_newest_notice_first_and_fills_old_snapshots(
    database: Database,
) -> None:
    start, _ = period_bounds("WEEKLY", (datetime.now(UTC) - timedelta(days=14)).date())
    async with database.sessions() as session:
        parent = User(phone="01012345678", name="Parent", role="PARENT")
        child = User(phone="01087654321", name="Child", role="CHILD")
        session.add_all([parent, child])
        await session.flush()
        ended = [
            datetime.combine(start + timedelta(days=offset), datetime.min.time(), tzinfo=UTC)
            + timedelta(hours=3)
            for offset in (1, 2, 3)
        ]
        calls = [
            CallRecord(
                parent_id=parent.id, child_id=child.id, room_name=f"room-{index}",
                state=CallState.ANALYZED.value, ended_at=ended_at,
            )
            for index, ended_at in enumerate(ended)
        ]
        session.add_all(calls)
        await session.flush()
        session.add_all([
            HealthExtraction(
                call_id=call.id, parse_status="OK",
                safety_notice=SAFETY_NOTICE if index != 1 else None,
            )
            for index, call in enumerate(calls)
        ])
        # safetyNotices 가 생기기 전의 지난 기간 스냅샷
        old_start = start - timedelta(days=7)
        session.add(Report(
            parent_id=parent.id, period="WEEKLY", from_date=old_start,
            to_date=old_start + timedelta(days=6), state="EMPTY",
            snapshot={"parentId": parent.id, "analyzedCallCount": 0},
        ))
        await session.commit()
        parent_id = parent.id
        call_ids = [call.id for call in calls]

    async with database.sessions() as session:
        report = await ReportService().get_or_issue(session, parent_id, "WEEKLY", start)
        old = await ReportService().get_or_issue(
            session, parent_id, "WEEKLY", start - timedelta(days=7)
        )

    assert [item["callId"] for item in report["safetyNotices"]] == [call_ids[2], call_ids[0]]
    assert report["safetyNotices"][0]["observedAt"] == ended[2].isoformat()
    assert old["safetyNotices"] == []


async def test_report_reissues_when_an_excluded_call_adds_a_notice(database: Database) -> None:
    # 짧은 통화는 분석된 통화 수를 바꾸지 않는다. 그래도 그 통화의 응급 안내가 생기면
    # 앞서 만든 스냅샷을 다시 만든다.
    async with database.sessions() as session:
        parent = User(phone="01012345678", name="Parent", role="PARENT")
        child = User(phone="01087654321", name="Child", role="CHILD")
        session.add_all([parent, child])
        await session.flush()
        call = CallRecord(
            parent_id=parent.id, child_id=child.id, room_name="short",
            state=CallState.ANALYSIS_EXCLUDED.value,
            ended_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session.add(call)
        await session.commit()
        parent_id, call_id = parent.id, call.id

    async with database.sessions() as session:
        first = await ReportService().get_or_issue(session, parent_id, "WEEKLY", None)
    assert first["safetyNotices"] == []

    async with database.sessions() as session:
        session.add(HealthExtraction(
            call_id=call_id, parse_status="EXCLUDED", safety_notice=SAFETY_NOTICE,
        ))
        await session.commit()

    async with database.sessions() as session:
        second = await ReportService().get_or_issue(session, parent_id, "WEEKLY", None)
        again = await ReportService().get_or_issue(session, parent_id, "WEEKLY", None)
    assert [item["callId"] for item in second["safetyNotices"]] == [call_id]
    assert second["analyzedCallCount"] == 0
    # 바뀐 것이 없으면 새로 만든 스냅샷을 그대로 쓴다.
    assert aware_datetime(again["issuedAt"]) == second["issuedAt"]
