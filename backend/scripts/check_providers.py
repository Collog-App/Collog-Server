from __future__ import annotations

import argparse
import asyncio
import io
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.services.deepgram import DeepgramSttGateway, SttError
from app.services.gemini import ExtractionError, GeminiExtractionGateway
from app.services.storage import LocalStorage
from app.services.tts import ElevenLabsQuestionTtsGateway, QuestionTtsError

Provider = Literal["gemini", "deepgram", "elevenlabs", "sms", "apns"]
PROVIDERS: tuple[Provider, ...] = ("gemini", "deepgram", "elevenlabs", "sms", "apns")


class CheckError(RuntimeError):
    pass


class GeminiModel(BaseModel):
    name: str
    methods: list[str] = Field(alias="supportedGenerationMethods")


class GeminiModels(BaseModel):
    models: list[GeminiModel]
    next_page_token: str | None = Field(default=None, alias="nextPageToken")


@dataclass(frozen=True)
class CheckResult:
    provider: str
    status: Literal["PASS", "FAIL", "CONFIG_ONLY", "SKIP"]
    detail: str


def require_settings(values: dict[str, object]) -> None:
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise CheckError("Missing " + ", ".join(missing))


async def verify_gemini_model(settings: Settings) -> None:
    expected = f"models/{settings.gemini_model}"
    params = {"pageSize": "1000"}
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(10):
            response = await client.get(
                f"{settings.gemini_base_url.rstrip('/')}/v1beta/models",
                headers={"x-goog-api-key": settings.gemini_api_key},
                params=params,
            )
            response.raise_for_status()
            page = GeminiModels.model_validate_json(response.content)
            for model in page.models:
                if model.name == expected:
                    if "generateContent" not in model.methods:
                        raise CheckError("Configured Gemini model does not support generateContent")
                    return
            if not page.next_page_token:
                raise CheckError("Configured Gemini model is unavailable to this API key")
            params["pageToken"] = page.next_page_token
    raise CheckError("Gemini model listing exceeded the page limit")


def synthetic_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 16_000)
    return stream.getvalue()


async def probe(provider: Provider, settings: Settings) -> CheckResult:
    if provider == "gemini":
        require_settings({"GEMINI_API_KEY": settings.gemini_api_key})
        await verify_gemini_model(settings)
        result = await GeminiExtractionGateway(settings).extract(
            "PARENT: 오늘 공원에서 산책했어요."
        )
        if not any(fact.category == "activity" for fact in result.facts):
            raise CheckError("Synthetic extraction did not return the expected activity fact")
        return CheckResult(provider, "PASS", "Model listed and synthetic extraction validated")
    if provider == "deepgram":
        require_settings({"DEEPGRAM_API_KEY": settings.deepgram_api_key})
        await DeepgramSttGateway(settings).transcribe(synthetic_wav(), "audio/wav", "PARENT")
        return CheckResult(
            provider, "PASS", "Model accepted synthetic silence, speech accuracy remains untested"
        )
    if provider == "elevenlabs":
        require_settings({
            "ELEVENLABS_API_KEY": settings.elevenlabs_api_key,
            "ELEVENLABS_VOICE_ID": settings.elevenlabs_voice_id,
        })
        with TemporaryDirectory(prefix="collog-provider-check-") as directory:
            isolated = settings.model_copy(update={"local_storage_path": Path(directory)})
            gateway = ElevenLabsQuestionTtsGateway(settings, LocalStorage(isolated))
            audio = await gateway.synthesize("콜로그 음성 서비스 테스트입니다.")
            if len(audio) < 100:
                raise CheckError("ElevenLabs returned an unexpectedly short audio response")
        return CheckResult(provider, "PASS", "Configured voice and model generated sample audio")
    if provider == "sms":
        if settings.sms_provider != "solapi":
            raise CheckError("SMS_PROVIDER must be solapi")
        require_settings({
            "SOLAPI_API_KEY": settings.solapi_api_key,
            "SOLAPI_API_SECRET": settings.solapi_api_secret,
            "SOLAPI_SENDER": settings.solapi_sender,
        })
        return CheckResult(provider, "CONFIG_ONLY", "SMS settings present, no message sent")
    if not settings.apns_voip_enabled:
        raise CheckError("APNS_VOIP_ENABLED must be true for background incoming calls")
    require_settings({
        "APNS_TEAM_ID": settings.apns_team_id,
        "APNS_KEY_ID": settings.apns_key_id,
        "APNS_BUNDLE_ID": settings.apns_bundle_id,
        "APNS_PRIVATE_KEY_PATH": settings.apns_private_key_path,
    })
    key_path = settings.apns_private_key_path
    if key_path is None or not key_path.is_file():
        raise CheckError("APNS_PRIVATE_KEY_PATH must reference an existing file")
    return CheckResult(provider, "CONFIG_ONLY", "APNs settings present, no notification sent")


def safe_error(exc: Exception) -> str:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, httpx.HTTPStatusError):
            return f"HTTP {cause.response.status_code}, verify credentials, model access and quota"
        if isinstance(cause, httpx.TimeoutException):
            return "Request timed out"
        if isinstance(cause, httpx.RequestError):
            return "Network request failed"
        cause = cause.__cause__
    if isinstance(exc, CheckError):
        return str(exc)
    return f"Invalid provider response or configuration ({type(exc).__name__})"


async def run_check(provider: Provider, settings: Settings) -> CheckResult:
    try:
        return await probe(provider, settings)
    except (CheckError, ExtractionError, SttError, QuestionTtsError, httpx.HTTPError,
            ValidationError, ValueError, OSError) as exc:
        return CheckResult(provider, "FAIL", safe_error(exc))


async def check(settings: Settings, selected: list[Provider] | None = None) -> int:
    providers: list[Provider] = selected or ["gemini", "deepgram", "sms", "apns"]
    skip_sms = selected is None and settings.apple_login_enabled and not any((
        settings.solapi_api_key, settings.solapi_api_secret, settings.solapi_sender,
    ))
    if skip_sms:
        providers.remove("sms")
    if selected is None and settings.question_tts_provider == "elevenlabs":
        providers.append("elevenlabs")
    results = await asyncio.gather(*(run_check(provider, settings) for provider in providers))
    for result in results:
        print(f"{result.provider} {result.status} {result.detail}")
    if skip_sms:
        print("sms SKIP Apple login is enabled and SMS credentials are not configured")
    if selected is None and settings.question_tts_provider != "elevenlabs":
        print("elevenlabs SKIP Remote TTS is disabled")
    print("Synthetic samples only. SMS and APNs delivery are never tested by this command.")
    return 1 if any(result.status == "FAIL" for result in results) else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check real provider access using synthetic samples"
    )
    parser.add_argument("--provider", choices=PROVIDERS, action="append")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError:
        print("FAIL Invalid environment settings, inspect field types without printing secrets")
        raise SystemExit(1) from None
    raise SystemExit(asyncio.run(check(settings, args.provider)))


if __name__ == "__main__":
    main()
