from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from google.protobuf.json_format import MessageToDict
from livekit import api

from app.config import Settings


class LiveKitError(RuntimeError):
    pass


@dataclass(slots=True)
class EgressStarted:
    egress_id: str
    object_key: str


class LiveKitGateway:
    async def create_room(self, room_name: str) -> None:
        raise NotImplementedError

    async def delete_room(self, room_name: str) -> None:
        raise NotImplementedError

    def participant_token(self, room_name: str, identity: str, name: str) -> str:
        raise NotImplementedError

    async def find_audio_track_id(self, room_name: str, identity: str) -> str | None:
        raise NotImplementedError

    async def start_track_egress(
        self, room_name: str, track_id: str, object_key: str
    ) -> EgressStarted:
        raise NotImplementedError

    async def stop_egress(self, egress_id: str) -> None:
        raise NotImplementedError

    def receive_webhook(self, body: str, authorization: str) -> dict:
        raise NotImplementedError


class MockLiveKitGateway(LiveKitGateway):
    async def create_room(self, room_name: str) -> None:
        del room_name

    async def delete_room(self, room_name: str) -> None:
        del room_name

    def participant_token(self, room_name: str, identity: str, name: str) -> str:
        del name
        return f"mock-livekit-token:{room_name}:{identity}"

    async def find_audio_track_id(self, room_name: str, identity: str) -> str | None:
        return f"mock-audio-track:{room_name}:{identity}"

    async def start_track_egress(
        self, room_name: str, track_id: str, object_key: str
    ) -> EgressStarted:
        del room_name, track_id
        return EgressStarted(egress_id=f"EG_{uuid.uuid4().hex}", object_key=object_key)

    async def stop_egress(self, egress_id: str) -> None:
        del egress_id

    def receive_webhook(self, body: str, authorization: str) -> dict:
        del authorization
        return json.loads(body)


class RealLiveKitGateway(LiveKitGateway):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.livekit_api_key or not settings.livekit_api_secret:
            raise LiveKitError("LiveKit API 키와 시크릿이 필요합니다")
        if settings.storage_backend != "s3":
            raise LiveKitError("LiveKit Egress 실사용에는 S3 호환 스토리지가 필요합니다")
        self.receiver = api.WebhookReceiver(
            api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
        )

    def _client(self) -> api.LiveKitAPI:
        return api.LiveKitAPI(
            self.settings.livekit_http_url,
            self.settings.livekit_api_key,
            self.settings.livekit_api_secret,
        )

    async def create_room(self, room_name: str) -> None:
        client = self._client()
        try:
            await client.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=60,
                    departure_timeout=20,
                    max_participants=4,
                )
            )
        except Exception as exc:
            raise LiveKitError("LiveKit room creation failed") from exc
        finally:
            await client.aclose()

    async def delete_room(self, room_name: str) -> None:
        client = self._client()
        try:
            await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except api.TwirpError as exc:
            if exc.code != "not_found":
                raise LiveKitError("LiveKit room cleanup failed") from exc
        except Exception as exc:
            raise LiveKitError("LiveKit room cleanup failed") from exc
        finally:
            await client.aclose()

    def participant_token(self, room_name: str, identity: str, name: str) -> str:
        return (
            api.AccessToken(self.settings.livekit_api_key, self.settings.livekit_api_secret)
            .with_identity(identity)
            .with_name(name)
            .with_ttl(timedelta(hours=2))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
            .to_jwt()
        )

    def _s3_upload(self) -> api.S3Upload:
        return api.S3Upload(
            access_key="" if self.settings.s3_use_instance_role else self.settings.s3_access_key_id,
            secret="" if self.settings.s3_use_instance_role else self.settings.s3_secret_access_key,
            region=self.settings.s3_region,
            endpoint=self.settings.s3_endpoint_url or "",
            bucket=self.settings.s3_bucket,
            force_path_style=self.settings.s3_force_path_style,
        )

    async def find_audio_track_id(self, room_name: str, identity: str) -> str | None:
        client = self._client()
        try:
            response = await client.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            participant = next(
                (item for item in response.participants if item.identity == identity), None
            )
            if participant is None:
                return None
            track = next(
                (
                    item
                    for item in participant.tracks
                    if item.type == api.TrackType.AUDIO
                    and item.source == api.TrackSource.MICROPHONE
                ),
                None,
            )
            return track.sid if track else None
        except Exception as exc:
            raise LiveKitError(f"LiveKit 오디오 트랙 조회 실패: {exc}") from exc
        finally:
            await client.aclose()

    def _track_egress_request(
        self, room_name: str, track_id: str, object_key: str
    ) -> api.TrackEgressRequest:
        return api.TrackEgressRequest(
            room_name=room_name,
            track_id=track_id,
            # Track Egress preserves the published Opus stream and therefore
            # produces OGG without running the Participant video transcode path.
            file=api.DirectFileOutput(
                filepath=object_key,
                disable_manifest=True,
                s3=self._s3_upload(),
            ),
        )

    async def start_track_egress(
        self, room_name: str, track_id: str, object_key: str
    ) -> EgressStarted:
        client = self._client()
        try:
            info = await client.egress.start_track_egress(
                self._track_egress_request(room_name, track_id, object_key)
            )
            return EgressStarted(egress_id=info.egress_id, object_key=object_key)
        except Exception as exc:  # provider error is normalized at this boundary
            raise LiveKitError(f"LiveKit Track Egress 시작 실패: {exc}") from exc
        finally:
            await client.aclose()

    async def stop_egress(self, egress_id: str) -> None:
        client = self._client()
        try:
            await client.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
        except Exception as exc:
            raise LiveKitError(f"LiveKit Egress 종료 실패: {exc}") from exc
        finally:
            await client.aclose()

    def receive_webhook(self, body: str, authorization: str) -> dict:
        try:
            event = self.receiver.receive(body, authorization)
        except Exception as exc:
            raise LiveKitError("LiveKit 웹훅 서명 검증에 실패했습니다") from exc
        return MessageToDict(event, preserving_proto_field_name=True)


def create_livekit_gateway(settings: Settings) -> LiveKitGateway:
    if settings.mock_external_services:
        return MockLiveKitGateway()
    return RealLiveKitGateway(settings)
