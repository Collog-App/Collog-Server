from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import tempfile
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

BACKEND = Path(__file__).resolve().parents[1]
LIVEKIT_PROBE = """import asyncio
import uuid

from livekit import api

from app.config import Settings
from app.services.livekit import RealLiveKitGateway


async def probe() -> None:
    settings = Settings()
    gateway = RealLiveKitGateway(settings)
    room = f"smoke-{uuid.uuid4().hex}"
    client = api.LiveKitAPI(
        settings.livekit_http_url, settings.livekit_api_key, settings.livekit_api_secret
    )
    try:
        await gateway.create_room(room)
        try:
            participants = await client.room.list_participants(
                api.ListParticipantsRequest(room=room)
            )
            if participants.participants:
                raise RuntimeError("The smoke room must have no participants")
            if await gateway.find_audio_track_id(room, "smoke-participant") is not None:
                raise RuntimeError("An empty smoke room must have no audio track")
        finally:
            await gateway.delete_room(room)
        rooms = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
        if rooms.rooms:
            raise RuntimeError("The smoke room was not deleted")
        await gateway.delete_room(room)
    finally:
        await client.aclose()
    print("LiveKit room creation, participant lookup, deletion, and repeated deletion passed.")


asyncio.run(probe())
"""


def text_field(value: object, path: tuple[str, ...]) -> str:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Missing Compose field {'.'.join(path)}")
        value = value[key]
    if not isinstance(value, str):
        raise ValueError(f"Expected text in Compose field {'.'.join(path)}")
    return value


def synthetic_environment(key: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
        if name in os.environ
    }
    for name in (
        "DB_PASSWORD", "JWT_SECRET", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "SOLAPI_API_KEY", "SOLAPI_API_SECRET",
        "DEEPGRAM_API_KEY", "GEMINI_API_KEY",
    ):
        environment[name] = secrets.token_hex(32)
    environment.update(
        API_DOMAIN="api.example.invalid",
        LIVEKIT_DOMAIN="rtc.example.invalid",
        ACME_EMAIL="test@example.invalid",
        APNS_KEY_FILE=str(key),
        APNS_ENVIRONMENT="sandbox",
        APNS_TEAM_ID="TESTTEAM00",
        APNS_KEY_ID="TESTKEY000",
        APNS_BUNDLE_ID="com.example.smoke",
        S3_ENDPOINT_URL="https://s3.example.invalid",
        S3_PUBLIC_ENDPOINT_URL="https://s3.example.invalid",
        S3_BUCKET="smoke-test",
        SOLAPI_SENDER="01000000000",
    )
    return environment


def run_stack(image: str) -> None:
    project = f"collog-stack-check-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="collog-stack-check-") as directory:
        root = Path(directory)
        key = root / "synthetic.p8"
        key.write_bytes(
            ec.generate_private_key(ec.SECP256R1()).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key.chmod(0o644)
        environment = synthetic_environment(key)
        base = [
            "docker", "compose", "--env-file", "/dev/null", "-p", project,
            "-f", str(BACKEND / "docker-compose.yml"),
        ]
        rendered = subprocess.run(
            [*base, "config", "--format", "json"], env=environment,
            check=True, capture_output=True, text=True, timeout=30,
        )
        document: object = json.loads(rendered.stdout)
        livekit = text_field(document, ("services", "livekit", "environment", "LIVEKIT_CONFIG"))
        environment["SMOKE_LIVEKIT_CONFIG"] = livekit.replace(
            "use_external_ip: true", "use_external_ip: false"
        )
        caddy = root / "Caddyfile"
        caddy.write_text(
            "{\n auto_https off\n}\n:8080 {\n reverse_proxy backend:8080\n}\n",
            encoding="utf-8",
        )
        environment["SMOKE_CADDY_FILE"] = str(caddy)
        environment["SMOKE_IMAGE"] = image
        override = root / "override.yaml"
        override.write_text(
            """services:
  postgres:
    volumes: !reset []
    tmpfs: [/var/lib/postgresql/data]
  redis:
    volumes: !reset []
  livekit:
    ports: !reset []
    environment:
      LIVEKIT_CONFIG: ${SMOKE_LIVEKIT_CONFIG}
  backend:
    image: ${SMOKE_IMAGE}
    pull_policy: never
  migrate:
    image: ${SMOKE_IMAGE}
    pull_policy: never
  caddy:
    ports: !reset []
    volumes: !override
      - type: bind
        source: ${SMOKE_CADDY_FILE}
        target: /etc/caddy/Caddyfile
        read_only: true
    tmpfs: [/data, /config]
""",
            encoding="utf-8",
        )
        command = [*base, "-f", str(override)]
        try:
            subprocess.run(
                [*command, "up", "-d", "--no-build", "--wait", "--wait-timeout", "120"],
                env=environment, check=True, timeout=300,
            )
            subprocess.run(
                [*command, "exec", "-T", "backend", "alembic", "check"],
                env=environment, check=True, timeout=30,
            )
            subprocess.run(
                [*command, "exec", "-T", "backend", "python", "-"],
                input=LIVEKIT_PROBE, text=True, env=environment, check=True, timeout=30,
            )
            subprocess.run(
                [*command, "exec", "-T", "caddy", "wget", "-q", "-O", "-",
                 "http://localhost:8080/v1/health"],
                env=environment, check=True, timeout=30,
            )
            print("\nProduction stack startup, schema, LiveKit API, and proxy health passed.")
        except (subprocess.SubprocessError, KeyboardInterrupt):
            subprocess.run(
                [*command, "logs", "--tail=30", "backend", "migrate", "egress", "livekit"],
                env=environment, check=False, timeout=30,
            )
            raise
        finally:
            subprocess.run(
                [*command, "down", "--volumes", "--remove-orphans"],
                env=environment, check=True, timeout=90,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="collog-deploy-check:local")
    arguments = parser.parse_args()
    run_stack(arguments.image)
