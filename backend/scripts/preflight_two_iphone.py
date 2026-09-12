from __future__ import annotations

import ipaddress
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def device_routable_url(name: str, value: str, schemes: set[str]) -> Check:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    loopback = host in {"localhost", "0.0.0.0", "::"}
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    passed = parsed.scheme in schemes and bool(host) and not loopback
    return Check(name, passed, f"scheme={parsed.scheme or '-'}, host={host or '-'}")


def configured(name: str, value: str | bool) -> Check:
    return Check(name, bool(value), "configured" if value else "missing")


def main() -> None:
    settings = Settings()
    checks = [
        Check(
            "real external providers",
            not settings.mock_external_services,
            f"MOCK_EXTERNAL_SERVICES={str(settings.mock_external_services).lower()}",
        ),
        configured("Deepgram key", settings.deepgram_api_key),
        configured("Gemini key", settings.gemini_api_key),
        configured("JWT secret", settings.jwt_secret),
        configured(
            "LiveKit API credentials",
            settings.livekit_api_key and settings.livekit_api_secret,
        ),
        device_routable_url(
            "PUBLIC_BASE_URL for iPhone",
            settings.public_base_url,
            {"http", "https"},
        ),
        device_routable_url("LIVEKIT_URL for iPhone", settings.livekit_url, {"ws", "wss"}),
        device_routable_url(
            "S3_PUBLIC_ENDPOINT_URL for iPhone",
            settings.s3_public_endpoint_url or "",
            {"http", "https"},
        ),
        Check(
            "S3/MinIO storage for dual Track Egress",
            settings.storage_backend == "s3",
            f"STORAGE_BACKEND={settings.storage_backend}",
        ),
        Check(
            "APNs VoIP enabled",
            settings.apns_voip_enabled,
            f"APNS_ENVIRONMENT={settings.apns_environment}",
        ),
        configured("APNs Team ID", settings.apns_team_id),
        configured("APNs Key ID", settings.apns_key_id),
        configured("APNs Bundle ID", settings.apns_bundle_id),
        Check(
            "APNs .p8 readable",
            bool(settings.apns_private_key_path)
            and Path(settings.apns_private_key_path).is_file(),
            "readable" if (
                settings.apns_private_key_path
                and Path(settings.apns_private_key_path).is_file()
            ) else "missing/unreadable",
        ),
    ]
    if settings.question_tts_provider in {"elevenlabs", "elevenlabs_direct"}:
        checks.extend(
            [
                Check(
                    "ElevenLabs actual API key",
                    settings.elevenlabs_api_key.startswith("sk_"),
                    (
                        "sk_ key configured"
                        if settings.elevenlabs_api_key.startswith("sk_")
                        else "missing or API key ID"
                    ),
                ),
                configured("ElevenLabs voice ID", settings.elevenlabs_voice_id),
            ]
        )
    else:
        checks.append(
            Check(
                "ElevenLabs selected",
                False,
                "QUESTION_TTS_PROVIDER=ios_local (통화는 가능하지만 server voice 미검증)",
            )
        )

    payload = {
        "passed": all(check.passed for check in checks),
        "checks": [asdict(check) for check in checks],
        "next": (
            "두 iPhone 설치와 고정 대화를 시작하세요."
            if all(check.passed for check in checks)
            else "failed 항목을 backend/.env 또는 backend/private에서 보완하세요."
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload["passed"] else 1)


if __name__ == "__main__":
    main()
