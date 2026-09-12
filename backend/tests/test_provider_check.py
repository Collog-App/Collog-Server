from __future__ import annotations

import io
import json
import wave

import httpx
import pytest

from app.config import Settings
from scripts.check_providers import check, run_check, safe_error, synthetic_wav


@pytest.mark.asyncio
async def test_missing_provider_credentials_fail_without_network(monkeypatch, capsys) -> None:
    def disallow_network(**kwargs):
        pytest.fail("Unconfigured provider attempted a network request")

    monkeypatch.setattr(httpx, "AsyncClient", disallow_network)
    settings = Settings(
        _env_file=None,
        gemini_api_key="",
        deepgram_api_key="",
        solapi_api_key="",
        apns_voip_enabled=False,
    )
    assert await check(settings) == 1
    output = capsys.readouterr().out
    assert "gemini FAIL Missing GEMINI_API_KEY" in output
    assert "deepgram FAIL Missing DEEPGRAM_API_KEY" in output
    assert "sms FAIL Missing" in output
    assert "apns FAIL" in output
    assert "elevenlabs SKIP" in output


@pytest.mark.asyncio
async def test_gemini_checks_real_http_even_when_mock_flag_is_set(monkeypatch) -> None:
    settings = Settings(
        _env_file=None, gemini_api_key="private-test-value", mock_external_services=True
    )
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["x-goog-api-key"] == settings.gemini_api_key
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{
                "name": f"models/{settings.gemini_model}",
                "supportedGenerationMethods": ["generateContent"],
            }]})
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        fact = {"category": "activity", "summary": "공원에서 산책함", "polarity": "PRESENT",
                "evidenceSegmentIds": ["s0000"]}
        return httpx.Response(200, json={"candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps({"facts": [fact]})}]},
        }]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs
    ))
    result = await run_check("gemini", settings)
    assert result.status == "PASS"
    assert paths == ["/v1beta/models", f"/v1beta/models/{settings.gemini_model}:generateContent"]


@pytest.mark.asyncio
async def test_unavailable_gemini_model_fails_without_fallback(monkeypatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"models": []})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs
    ))
    settings = Settings(_env_file=None, gemini_api_key="private-test-value")
    result = await run_check("gemini", settings)
    assert result.status == "FAIL"
    assert "unavailable" in result.detail


def test_error_output_does_not_include_provider_url_or_key() -> None:
    request = httpx.Request("GET", "https://example.test/models?key=private-test-value")
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError("private-test-value", request=request, response=response)
    assert safe_error(error) == "HTTP 403, verify credentials, model access and quota"


@pytest.mark.asyncio
async def test_sms_and_apns_only_check_configuration(monkeypatch, tmp_path) -> None:
    def disallow_network(**kwargs):
        pytest.fail("Configuration checks must not send messages")

    monkeypatch.setattr(httpx, "AsyncClient", disallow_network)
    key_path = tmp_path / "test.p8"
    key_path.touch()
    settings = Settings(
        _env_file=None,
        sms_provider="solapi",
        solapi_api_key="private-test-value",
        solapi_api_secret="private-test-secret",
        solapi_sender="0212345678",
        apns_voip_enabled=True,
        apns_team_id="TEAM",
        apns_key_id="KEY",
        apns_bundle_id="com.test.app",
        apns_private_key_path=key_path,
    )
    for provider in ("sms", "apns"):
        result = await run_check(provider, settings)
        assert result.status == "CONFIG_ONLY"


def test_stt_sample_is_a_one_second_pcm_wav() -> None:
    with wave.open(io.BytesIO(synthetic_wav()), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getframerate() == 16_000
        assert audio.getnframes() == 16_000
        assert audio.getsampwidth() == 2
