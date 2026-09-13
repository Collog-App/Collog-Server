from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.models import (
    AudioAsset,
    CallRecord,
    ConsentRecord,
    Device,
    Family,
    FamilyMember,
    Invitation,
    ParentProfile,
    RefreshSession,
    Report,
    Transcript,
    User,
)
from app.services.storage import StorageError
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_auth import login


def test_role_change_preserves_family_and_reverses_call_roles(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    for token, role in ((child_token, "PARENT"), (parent_token, "CHILD")):
        changed = client.patch("/v1/account/role", headers=auth(token), json={"role": role})
        assert changed.status_code == 200, changed.text
        assert changed.json()["role"] == role
        assert changed.json()["familyId"] == child["familyId"]
    members = client.get(
        f"/v1/families/{child['familyId']}/members", headers=auth(parent_token)
    ).json()["members"]
    assert {member["userId"]: member["role"] for member in members} == {
        child["id"]: "PARENT", parent["id"]: "CHILD",
    }
    call = client.post("/v1/calls", headers=auth(parent_token), json={"calleeId": child["id"]})
    assert call.status_code == 201, call.text
    async def check() -> None:
        async with client.app.state.container.database.sessions() as session:
            stored = await session.get(CallRecord, call.json()["callId"])
            assert stored.parent_id == child["id"]
            assert stored.child_id == parent["id"]

    client.portal.call(check)


def test_role_change_keeps_history_and_current_consent(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)

    async def seed() -> str:
        async with client.app.state.container.database.sessions() as session:
            call = CallRecord(
                parent_id=parent["id"], child_id=child["id"], room_name="history",
                state="ANALYZED", ended_at=datetime.now(UTC),
            )
            session.add(call)
            await session.commit()
            return call.id

    call_id = client.portal.call(seed)
    for token, role in ((child_token, "PARENT"), (parent_token, "CHILD")):
        assert client.patch(
            "/v1/account/role", headers=auth(token), json={"role": role}
        ).status_code == 200
    members = client.get(
        f"/v1/families/{child['familyId']}/members", headers=auth(child_token)
    ).json()["members"]
    assert next(member for member in members if member["userId"] == child["id"])[
        "status"
    ] == "CONSENT_GRANTED"

    async def check() -> None:
        async with client.app.state.container.database.sessions() as session:
            call = await session.get(CallRecord, call_id)
            assert (call.parent_id, call.child_id) == (parent["id"], child["id"])

    client.portal.call(check)


def test_new_child_gets_family_and_invalid_role_is_rejected(client: TestClient) -> None:
    token, _ = create_user(client, "01098765432", "PARENT", "테스트")
    changed = client.patch("/v1/account/role", headers=auth(token), json={"role": "CHILD"})
    assert changed.status_code == 200
    assert changed.json()["familyId"]
    assert client.patch(
        "/v1/account/role", headers=auth(token), json={"role": "ADMIN"}
    ).status_code == 422
    assert client.delete("/v1/account").status_code == 401


@pytest.mark.parametrize("state", ["CREATED", "RINGING", "ACTIVE", "ENDED", "PROCESSING"])
def test_delete_waits_for_calls_and_processing(client: TestClient, state: str) -> None:
    child_token, child, _, parent = onboard_family(client)

    async def seed() -> None:
        async with client.app.state.container.database.sessions() as session:
            session.add(CallRecord(
                parent_id=parent["id"], child_id=child["id"], room_name="busy", state=state,
            ))
            await session.commit()

    client.portal.call(seed)
    assert client.delete("/v1/account", headers=auth(child_token)).status_code == 409
    if state in {"CREATED", "RINGING", "ACTIVE"}:
        assert client.patch(
            "/v1/account/role", headers=auth(child_token), json={"role": "PARENT"}
        ).status_code == 409


@pytest.mark.parametrize("delete_parent", [True, False])
def test_delete_removes_private_data_and_preserves_other_user(
    client: TestClient, delete_parent: bool
) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    deleted_user = parent if delete_parent else child
    token = parent_token if delete_parent else child_token
    container = client.app.state.container

    async def seed() -> str:
        async with container.database.sessions() as session:
            await session.execute(text("PRAGMA foreign_keys = ON"))
            assert await session.scalar(text("PRAGMA foreign_keys")) == 1
            call = CallRecord(
                parent_id=parent["id"], child_id=child["id"], room_name="private",
                state="ANALYZED", ended_at=datetime.now(UTC),
            )
            session.add(call)
            await session.flush()
            key = f"calls/{call.id}/parent.wav"
            await container.storage.write(key, b"private audio")
            session.add_all([
                    AudioAsset(
                        call_id=call.id, kind="DEVICE_RAW", uri=f"local://{key}", status="UPLOADED",
                        created_at=datetime.now(UTC) - timedelta(days=1),
                ),
                Transcript(call_id=call.id, provider="test", segments=[{"text": "private"}]),
                ParentProfile(parent_id=parent["id"], conditions=["OTHER"]),
                Report(
                    parent_id=parent["id"], period="WEEKLY", from_date=date.today(),
                    to_date=date.today(), state="READY", snapshot={"private": "summary"},
                ),
                Device(user_id=deleted_user["id"], platform="IOS", token="device"),
            ])
            await session.commit()
            return key

    key = client.portal.call(seed)
    response = client.delete("/v1/account", headers=auth(token))
    assert response.status_code == 204, response.text
    assert client.get(
        f"/v1/families/{child['familyId']}/members", headers=auth(token)
    ).status_code == 401

    async def check() -> None:
        async with container.database.sessions() as session:
            assert await session.get(User, deleted_user["id"]) is None
            assert await session.scalar(select(func.count(User.id))) == 1
            for model in (
                CallRecord, AudioAsset, Transcript, Report, Device, FamilyMember, Invitation,
            ):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
            assert await session.scalar(select(func.count(Family.id))) == int(delete_parent)
            for model in (RefreshSession, ConsentRecord):
                assert await session.scalar(
                    select(func.count()).select_from(model)
                    .where(model.user_id == deleted_user["id"])
                ) == 0
            assert not await container.storage.exists(f"local://{key}")

    client.portal.call(check)


def test_delete_invalidates_refresh_and_allows_new_registration(client: TestClient) -> None:
    credentials = login(client)
    assert client.delete(
        "/v1/account", headers=auth(credentials["accessToken"])
    ).status_code == 204
    assert client.post(
        "/v1/auth/refresh", json={"refreshToken": credentials["refreshToken"]}
    ).status_code == 401
    assert login(client)["user"]["id"] != credentials["user"]["id"]


def test_storage_failure_keeps_account_for_retry(client: TestClient, monkeypatch) -> None:
    child_token, child, _, parent = onboard_family(client)
    container = client.app.state.container

    async def seed() -> None:
        async with container.database.sessions() as session:
            call = CallRecord(
                parent_id=parent["id"], child_id=child["id"], room_name="storage-error",
                state="ANALYZED", ended_at=datetime.now(UTC),
            )
            session.add(call)
            await session.flush()
            session.add(AudioAsset(
                call_id=call.id, kind="DEVICE_RAW", uri="local://private.wav",
                created_at=datetime.now(UTC) - timedelta(days=1),
            ))
            await session.commit()

    async def fail_delete(uri: str) -> None:
        raise StorageError("offline")

    client.portal.call(seed)
    monkeypatch.setattr(container.storage, "delete", fail_delete)
    assert client.delete("/v1/account", headers=auth(child_token)).status_code == 503
    assert client.get(
        f"/v1/families/{child['familyId']}/members", headers=auth(child_token)
    ).status_code == 200


def test_delete_waits_until_signed_upload_urls_expire(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)

    async def seed() -> None:
        async with client.app.state.container.database.sessions() as session:
            call = CallRecord(
                parent_id=parent["id"], child_id=child["id"], room_name="late-upload",
                state="ANALYSIS_FAILED", ended_at=datetime.now(UTC),
            )
            session.add(call)
            await session.flush()
            session.add(AudioAsset(call_id=call.id, kind="DEVICE_RAW", uri="local://late.wav"))
            await session.commit()

    client.portal.call(seed)
    assert client.delete("/v1/account", headers=auth(child_token)).status_code == 409
