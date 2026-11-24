from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import CallRecord
from app.schemas import QuestionTtsToken
from app.services import usage
from app.services.deepgram import DeepgramSttGateway
from app.services.gemini import GeminiExtractionGateway
from app.services.storage import LocalStorage
from app.services.tts import ElevenLabsQuestionTtsGateway
from tests.conftest import auth
from tests.test_api_flow import onboard_family
from tests.test_call_summary import LONG_TALK, TALK
from tests.test_direct_tts import prepare_call
from tests.test_guardrail import analyze_family_call
from tests.test_tts import question, settings

CHILD_TALK = "CHILD: 쓰러지신 적은 없으시죠?"  # analyze_family_call 의 자녀 트랙


def saved_usage(client: TestClient, call_id: str) -> dict | None:
    async def read() -> dict | None:
        async with client.app.state.container.database.sessions() as session:
            return (await session.get(CallRecord, call_id)).ai_usage

    return client.portal.call(read)


def call_state(client: TestClient, call_id: str) -> str:
    async def read() -> str:
        async with client.app.state.container.database.sessions() as session:
            return (await session.get(CallRecord, call_id)).state

    return client.portal.call(read)


# ------------------------------------------------------------ 합치기 규칙


def test_merge_sums_and_keeps_unknown_values() -> None:
    assert usage.merge(None, {"sttAudioSec": 1.5}) == {"sttAudioSec": 1.5}
    assert usage.merge({"sttAudioSec": 1.5, "ttsCharacters": 0}, {"sttAudioSec": 2.25}) == {
        "sttAudioSec": 3.75,
        "ttsCharacters": 0,
    }
    # 한 번이라도 값을 모르면 합도 모른다
    assert usage.merge({"geminiInputTokens": 10}, {"geminiInputTokens": None}) == {
        "geminiInputTokens": None
    }
    assert usage.merge({"geminiInputTokens": None}, {"geminiInputTokens": 5}) == {
        "geminiInputTokens": None
    }


def test_values_outside_a_meter_or_not_numbers_are_ignored_or_unknown() -> None:
    usage.add("sttAudioSec", 3.0)  # 계량 중이 아니면 남기지 않는다(스크립트·점검)
    with usage.metering() as spent:
        usage.add("sttAudioSec", "3")
        usage.add("ttsCharacters", True)
        usage.add("geminiInputTokens", -1)
        usage.add_gemini("not a mapping")
    assert spent == {
        "sttAudioSec": None,
        "ttsCharacters": None,
        "geminiInputTokens": None,
        "geminiOutputTokens": None,
        "geminiThoughtTokens": None,
    }


# ------------------------------------------------------------ 공급자 응답


async def test_deepgram_duration_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [{"metadata": {"duration": 12.5}}, {"metadata": {}}]

    async def fake_request(client, method, url, **kwargs):
        del client, method, url, kwargs
        reply = replies.pop(0)
        return httpx.Response(200, json={
            **reply,
            "results": {"channels": [{"alternatives": [{"transcript": "", "words": []}]}]},
        })

    monkeypatch.setattr("app.services.deepgram.request_with_retry", fake_request)
    gateway = DeepgramSttGateway(Settings(mock_external_services=False, deepgram_api_key="test"))
    with usage.metering() as spent:
        await gateway.transcribe(b"audio", "audio/ogg", "PARENT")
    assert spent == {"sttAudioSec": 12.5}
    with usage.metering() as spent:
        await gateway.transcribe(b"audio", "audio/ogg", "PARENT")
    assert spent == {"sttAudioSec": None}


async def test_gemini_usage_is_summed_over_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = [
        {"promptTokenCount": 300, "candidatesTokenCount": 40, "thoughtsTokenCount": 120},
        # 생각 토큰이 0 이면 응답에서 빠진다
        {"promptTokenCount": 200, "candidatesTokenCount": 30},
        None,
    ]

    async def fake_request(client, method, url, **kwargs):
        del client, method, url, kwargs
        body = {"candidates": [{"content": {"parts": [{"text": '{"summary": "산책 이야기"}'}]}}]}
        item = metadata.pop(0)
        if item is not None:
            body["usageMetadata"] = item
        return httpx.Response(200, json=body)

    monkeypatch.setattr("app.services.gemini.request_with_retry", fake_request)
    gateway = GeminiExtractionGateway(
        Settings(mock_external_services=False, gemini_api_key="test", guardrail_llm_review=False)
    )
    with usage.metering() as spent:
        await gateway.summarize(TALK)
        await gateway.summarize(TALK)
    assert spent == {
        "geminiInputTokens": 500,
        "geminiOutputTokens": 190,
        "geminiThoughtTokens": 120,
    }
    with usage.metering() as spent:
        await gateway.summarize(TALK)
    assert spent == {
        "geminiInputTokens": None,
        "geminiOutputTokens": None,
        "geminiThoughtTokens": None,
    }


async def test_elevenlabs_characters_come_from_the_response_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = [{"character-cost": "14"}, {}]

    async def fake_request(client, method, url, **kwargs):
        del client, method, url, kwargs
        return httpx.Response(200, content=b"ID3-audio", headers=headers.pop(0))

    monkeypatch.setattr("app.services.tts.request_with_retry", fake_request)
    config = settings(tmp_path)
    gateway = ElevenLabsQuestionTtsGateway(config, LocalStorage(config))
    with usage.metering() as spent:
        await gateway.attach_audio([question()])
    assert spent == {"ttsCharacters": 14}
    with usage.metering() as spent:  # 만들어 둔 음성을 다시 쓰면 요청이 없다
        await gateway.attach_audio([question()])
    assert spent == {"ttsCharacters": 0}

    other = question().model_copy(update={"question_id": "walk-01", "text": "오늘 걸으셨어요?"})
    with usage.metering() as spent:  # 응답에 글자 수가 없으면 추정하지 않는다
        await gateway.attach_audio([other])
    assert spent == {"ttsCharacters": None}


# ------------------------------------------------------------ 통화에 남기기


def test_analyzed_call_keeps_the_mock_usage_of_each_provider(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    call_id = analyze_family_call(client, child_token, parent_token, parent, LONG_TALK)
    spent = saved_usage(client, call_id)
    # 가짜 전사는 글자 4개를 1초로 친다. 부모·자녀 트랙을 따로 보낸다.
    assert spent["sttAudioSec"] == pytest.approx(len(LONG_TALK) / 4 + len(CHILD_TALK) / 4)
    # 가짜 Gemini 는 요청마다 입력 100·답 20·생각 5 토큰을 돌려준다(추출 + 요약 = 두 번).
    assert spent["geminiInputTokens"] == 200
    assert spent["geminiOutputTokens"] == 50
    assert spent["geminiThoughtTokens"] == 10
    assert "ttsCharacters" not in spent  # 휴대폰 기본 음성(ios_local)


def test_failed_analysis_still_keeps_what_was_spent(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)

    class BrokenAcoustics:
        async def analyze(self, payload):
            del payload
            raise ValueError("analyzer crashed")

    client.app.state.container.pipeline.acoustics = BrokenAcoustics()
    call_id = analyze_family_call(
        client, child_token, parent_token, parent, LONG_TALK, state="ANALYSIS_FAILED"
    )
    assert saved_usage(client, call_id)["sttAudioSec"] > 0


@pytest.mark.parametrize(
    ("where", "logged"),
    [("recording", "AI usage could not be recorded"), ("saving", "AI usage record failed")],
)
def test_usage_record_failure_does_not_block_analysis(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
    logged: str,
) -> None:
    def broken_merge(old, new):
        raise RuntimeError("usage store broken")

    if where == "recording":  # 공급자 응답을 더하다 실패
        monkeypatch.setattr(usage, "merge", broken_merge)
    else:  # 다 모은 뒤 통화에 저장하다 실패
        monkeypatch.setattr(
            "app.services.pipeline.usage",
            SimpleNamespace(metering=usage.metering, merge=broken_merge),
        )
    child_token, _, parent_token, parent = onboard_family(client)
    call_id = analyze_family_call(client, child_token, parent_token, parent, LONG_TALK)
    assert call_state(client, call_id) == "ANALYZED"
    assert saved_usage(client, call_id) is None
    assert logged in caplog.text


def test_server_made_question_voice_counts_for_the_new_call(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_request(client_, method, url, **kwargs):
        del client_, method, url
        text = kwargs["json"]["text"]
        return httpx.Response(200, content=b"ID3", headers={"character-cost": str(len(text))})

    monkeypatch.setattr("app.services.tts.request_with_retry", fake_request)
    config = settings(tmp_path)
    client.app.state.container.question_tts = ElevenLabsQuestionTtsGateway(
        config, LocalStorage(config)
    )
    child_token, _, _, parent = onboard_family(client)
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201, created.text
    texts = {item["text"] for item in created.json()["questions"]}
    assert texts
    assert saved_usage(client, created.json()["callId"]) == {
        "ttsCharacters": sum(len(text) for text in texts)
    }


def test_direct_question_voice_leaves_the_characters_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, child_token, _, _, call, route = prepare_call(client)

    async def issue():
        return QuestionTtsToken(
            token="sutkn_test", voice_id="test-voice", model_id="eleven_flash_v2_5",
            output_format="mp3_44100_128",
        )

    monkeypatch.setattr(gateway, "issue_token", issue)
    assert saved_usage(client, call["callId"]) is None
    assert client.post(route, headers=auth(child_token)).status_code == 200
    assert saved_usage(client, call["callId"]) == {"ttsCharacters": None}
