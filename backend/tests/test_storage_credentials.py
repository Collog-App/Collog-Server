from urllib.parse import parse_qs, urlparse

import boto3
import pytest

from app.config import Settings
from app.services.livekit import RealLiveKitGateway
from app.services.storage import S3Storage


@pytest.mark.parametrize("use_instance_role", [False, True])
async def test_s3_presigning_uses_selected_credentials(
    monkeypatch: pytest.MonkeyPatch, use_instance_role: bool
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "role-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "role-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "role-session")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    settings = Settings(
        _env_file=None,
        livekit_api_key="test-key",
        livekit_api_secret="test-secret",
        storage_backend="s3",
        s3_bucket="test-audio",
        s3_use_instance_role=use_instance_role,
        s3_access_key_id="" if use_instance_role else "static-access",
        s3_secret_access_key="" if use_instance_role else "static-secret",
        s3_endpoint_url="https://s3.ap-northeast-2.amazonaws.com",
        s3_public_endpoint_url="https://s3.ap-northeast-2.amazonaws.com",
    )
    storage = S3Storage(settings)
    urls = [
        await storage.create_upload_url("calls/test.wav", "audio/wav"),
        await storage.create_download_url("s3://test-audio/calls/test.wav"),
    ]
    for url in urls:
        query = parse_qs(urlparse(url).query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        credential = "role-access" if use_instance_role else "static-access"
        assert query["X-Amz-Credential"][0].startswith(f"{credential}/")
        if use_instance_role:
            assert query["X-Amz-Security-Token"] == ["role-session"]
        else:
            assert "X-Amz-Security-Token" not in query
    output = RealLiveKitGateway(settings)._s3_upload()
    assert output.bucket == "test-audio"
    assert output.access_key == settings.s3_access_key_id
    assert output.secret == settings.s3_secret_access_key
