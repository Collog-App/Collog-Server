from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models import CallRecord, QuestionTtsGrant
from app.schemas import QuestionTtsToken
from app.services.tts import ElevenLabsDirectTtsGateway, QuestionTtsError
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_tts import question, settings


def prepare_call(client: TestClient):
    child_token, child, parent_token, parent = onboard_family(client)
    config = client.app.state.container.settings.model_copy(update={
        "question_tts_provider": "elevenlabs_direct",
        "elevenlabs_api_key": "sk_test-only",
        "elevenlabs_voice_id": "test-voice",
    })
    gateway = ElevenLabsDirectTtsGateway(config)
    client.app.state.container.question_tts = gateway
    created = client.post(
        "/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]}
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert all(item["ttsMode"] == "ELEVENLABS_DIRECT" for item in payload["questions"])
    question_id = payload["questions"][0]["questionId"]
    route = f"/v1/calls/{payload['callId']}/questions/{question_id}/tts-token"
    return gateway, child_token, child, parent_token, payload, route


def test_token_access_and_per_question_limit(client, monkeypatch):
    gateway, child_token, _, parent_token, call, route = prepare_call(client)
    issued = []

    async def issue():
        issued.append(True)
        return QuestionTtsToken(
            token="sutkn_test", voice_id="test-voice", model_id="eleven_flash_v2_5",
            output_format="mp3_44100_128",
        )

    monkeypatch.setattr(gateway, "issue_token", issue)
    assert client.post(route).status_code == 401
    assert client.post(route, headers=auth(parent_token)).status_code == 403
    outsider, _ = create_user(client, "01099998888", "CHILD", "다른 사람")
    assert client.post(route, headers=auth(outsider)).status_code == 403
    missing = f"/v1/calls/{call['callId']}/questions/not-a-question/tts-token"
    assert client.post(missing, headers=auth(child_token)).status_code == 404
    assert not issued
    for _ in range(2):
        response = client.post(route, headers=auth(child_token))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "token": "sutkn_test", "voiceId": "test-voice", "modelId": "eleven_flash_v2_5",
            "outputFormat": "mp3_44100_128", "expiresIn": 900,
        }
        assert "sk_test" not in response.text
    assert client.post(route, headers=auth(child_token)).status_code == 429
    assert len(issued) == 2


@pytest.mark.parametrize("expired", [False, True])
def test_token_rejects_answered_or_expired_call(client, expired):
    _, child_token, _, parent_token, call, route = prepare_call(client)
    if expired:
        async def expire():
            async with client.app.state.container.database.sessions() as session:
                record = await session.get(CallRecord, call["callId"])
                record.started_at = datetime.now(UTC) - timedelta(hours=1)
                await session.commit()
        client.portal.call(expire)
    else:
        response = client.post(f"/v1/calls/{call['callId']}/accept", headers=auth(parent_token))
        assert response.status_code == 200
    assert client.post(route, headers=auth(child_token)).status_code == (410 if expired else 409)


def test_token_enforces_hourly_user_limit(client):
    _, child_token, child, _, call, route = prepare_call(client)

    async def exhaust():
        async with client.app.state.container.database.sessions() as session:
            session.add_all([
                QuestionTtsGrant(user_id=child["id"], call_id=call["callId"], question_id="other")
                for _ in range(20)
            ])
            await session.commit()
    client.portal.call(exhaust)
    assert client.post(route, headers=auth(child_token)).status_code == 429


def test_token_provider_failure_uses_retry_budget(client, monkeypatch):
    gateway, child_token, _, _, _, route = prepare_call(client)

    async def fail():
        raise QuestionTtsError("음성 토큰 발급 실패")
    monkeypatch.setattr(gateway, "issue_token", fail)
    assert client.post(route, headers=auth(child_token)).status_code == 502
    assert client.post(route, headers=auth(child_token)).status_code == 502
    assert client.post(route, headers=auth(child_token)).status_code == 429


async def test_direct_provider_only_creates_single_use_token(tmp_path, monkeypatch):
    config = settings(tmp_path)
    gateway = ElevenLabsDirectTtsGateway(config)
    requests = []

    async def post(self, url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"token": "sutkn_test"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    questions = await gateway.attach_audio([question()])
    assert requests == []
    assert questions[0].tts_mode == "ELEVENLABS_DIRECT"
    assert questions[0].tts_asset_url is None
    token = await gateway.issue_token()
    assert token.token == "sutkn_test"
    assert len(requests) == 1
    assert requests[0][0].endswith("/v1/single-use-token/tts_websocket")
    assert requests[0][1] == {"headers": {"xi-api-key": config.elevenlabs_api_key}}


@pytest.mark.parametrize("payload", [{}, {"token": 42}, {"token": " "}, []])
async def test_direct_provider_rejects_invalid_tokens(tmp_path, monkeypatch, payload):
    async def post(self, url, **kwargs):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    gateway = ElevenLabsDirectTtsGateway(settings(tmp_path))
    with pytest.raises(QuestionTtsError):
        await gateway.issue_token()
