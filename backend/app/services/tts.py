from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import quote

import httpx

from app.config import Settings
from app.schemas import QuestionTtsToken, QuestionView
from app.services.http import request_with_retry
from app.services.storage import StorageGateway

logger = logging.getLogger(__name__)


class QuestionTtsError(RuntimeError):
    pass


class QuestionTtsGateway:
    provider = "ios-local"
    configured = True

    async def attach_audio(self, questions: list[QuestionView]) -> list[QuestionView]:
        return questions


class ElevenLabsQuestionTtsGateway(QuestionTtsGateway):
    provider = "elevenlabs"
    configured = True

    def __init__(self, settings: Settings, storage: StorageGateway) -> None:
        if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
            raise QuestionTtsError("ElevenLabs API key와 voice ID가 필요합니다")
        if not settings.elevenlabs_api_key.startswith("sk_"):
            raise QuestionTtsError(
                "ElevenLabs API key ID가 아닌 sk_로 시작하는 실제 key가 필요합니다"
            )
        if not settings.elevenlabs_output_format.startswith("mp3_"):
            raise QuestionTtsError("현재 iOS remote TTS는 ElevenLabs MP3 output만 지원합니다")
        self.settings = settings
        self.storage = storage
        self._locks: dict[str, asyncio.Lock] = {}

    async def attach_audio(self, questions: list[QuestionView]) -> list[QuestionView]:
        results = await asyncio.gather(
            *(self._attach_one(question) for question in questions),
            return_exceptions=True,
        )
        attached: list[QuestionView] = []
        for question, result in zip(questions, results, strict=True):
            if isinstance(result, Exception):
                # TTS는 통화 연결의 필수 조건이 아니다. provider/storage 장애 때에는 질문 text와
                # iOS voice를 그대로 사용해 통화 생성을 성공시킨다.
                logger.warning(
                    "ElevenLabs question TTS failed; falling back to iOS local voice: %s",
                    result,
                )
                attached.append(question)
            else:
                attached.append(result)
        return attached

    async def _attach_one(self, question: QuestionView) -> QuestionView:
        cache_key = self._cache_key(question)
        uri = self.storage.object_uri(cache_key)
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            if not await self.storage.exists(uri):
                audio = await self.synthesize(question.text)
                await self.storage.write(cache_key, audio, "audio/mpeg")
        url = await self.storage.create_download_url(uri)
        return question.model_copy(
            update={
                "tts_asset_url": url,
                "tts_mode": "REMOTE_ASSET",
            }
        )

    async def synthesize(self, text: str) -> bytes:
        body: dict[str, object] = {
            "text": text,
            "model_id": self.settings.elevenlabs_model,
        }
        # Official API documents language_code for supported models, but explicitly excludes
        # eleven_multilingual_v2. Flash v2.5 accepts ko and is the default low-latency model.
        if self.settings.elevenlabs_model != "eleven_multilingual_v2":
            body["language_code"] = "ko"
        url = (
            f"{self.settings.elevenlabs_base_url.rstrip('/')}/v1/text-to-speech/"
            f"{quote(self.settings.elevenlabs_voice_id, safe='')}"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await request_with_retry(
                    client,
                    "POST",
                    url,
                    params={"output_format": self.settings.elevenlabs_output_format},
                    headers={
                        "xi-api-key": self.settings.elevenlabs_api_key,
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise QuestionTtsError(f"ElevenLabs TTS 요청 실패: {exc}") from exc
        if not response.content:
            raise QuestionTtsError("ElevenLabs가 빈 오디오를 반환했습니다")
        return response.content

    def _cache_key(self, question: QuestionView) -> str:
        identity = "\n".join(
            (
                self.settings.elevenlabs_voice_id,
                self.settings.elevenlabs_model,
                self.settings.elevenlabs_output_format,
                question.text,
            )
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return f"tts/questions/{question.question_id}-{digest}.mp3"


class ElevenLabsDirectTtsGateway(QuestionTtsGateway):
    provider = "elevenlabs-direct"

    def __init__(self, settings: Settings) -> None:
        if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
            raise QuestionTtsError("ElevenLabs API key와 voice ID가 필요합니다")
        if not settings.elevenlabs_output_format.startswith("mp3_"):
            raise QuestionTtsError("iOS 직접 재생에는 MP3 output이 필요합니다")
        if settings.elevenlabs_model == "eleven_v3":
            raise QuestionTtsError("직접 질문 재생에는 Flash 또는 Multilingual 모델이 필요합니다")
        self.settings = settings

    async def attach_audio(self, questions: list[QuestionView]) -> list[QuestionView]:
        return [
            question.model_copy(update={"tts_mode": "ELEVENLABS_DIRECT", "tts_asset_url": None})
            for question in questions
        ]

    async def issue_token(self) -> QuestionTtsToken:
        url = f"{self.settings.elevenlabs_base_url.rstrip('/')}/v1/single-use-token/tts_websocket"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url, headers={"xi-api-key": self.settings.elevenlabs_api_key}
                )
                response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QuestionTtsError("ElevenLabs 음성 토큰을 발급하지 못했습니다") from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise QuestionTtsError("ElevenLabs 음성 토큰 응답이 올바르지 않습니다")
        return QuestionTtsToken(
            token=token,
            voice_id=self.settings.elevenlabs_voice_id,
            model_id=self.settings.elevenlabs_model,
            output_format=self.settings.elevenlabs_output_format,
        )


def create_question_tts_gateway(
    settings: Settings, storage: StorageGateway
) -> QuestionTtsGateway:
    if settings.question_tts_provider == "ios_local" or settings.mock_external_services:
        return QuestionTtsGateway()
    try:
        if settings.question_tts_provider == "elevenlabs_direct":
            return ElevenLabsDirectTtsGateway(settings)
        return ElevenLabsQuestionTtsGateway(settings, storage)
    except QuestionTtsError as exc:
        logger.warning("ElevenLabs TTS is not configured; using iOS local voice: %s", exc)
        return QuestionTtsGateway()
