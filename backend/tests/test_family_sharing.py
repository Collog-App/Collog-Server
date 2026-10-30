from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import FamilyShare
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_auth import login
from tests.test_family_symmetric import accept, invite

REPORT_PATHS = ("reports?period=WEEKLY", "baseline", "signals")


def sharing(client: TestClient, token: str) -> list[dict]:
    response = client.get("/v1/account/sharing", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()["shares"]


def put_sharing(
    client: TestClient, token: str, viewer_id: str, report: bool, timeline: bool
) -> int:
    response = client.put(
        "/v1/account/sharing",
        headers=auth(token),
        json={"shares": [{"viewerUserId": viewer_id, "report": report, "timeline": timeline}]},
    )
    if response.status_code == 200:
        assert response.json()["shares"] == sharing(client, token)
    return response.status_code


def remove(client: TestClient, token: str, family_id: str, member_id: str) -> int:
    url = f"/v1/families/{family_id}/members/{member_id}"
    return client.delete(url, headers=auth(token)).status_code


def status_of(client: TestClient, token: str, subject_id: str, path: str) -> int:
    return client.get(f"/v1/parents/{subject_id}/{path}", headers=auth(token)).status_code


def members(client: TestClient, token: str, family_id: str) -> dict[str | None, dict]:
    response = client.get(f"/v1/families/{family_id}/members", headers=auth(token))
    assert response.status_code == 200, response.text
    return {row["userId"] or row["memberId"]: row for row in response.json()["members"]}


def test_sharing_defaults_to_everything_for_each_connected_member(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    invite(client, child_token, child["familyId"], "FATHER")
    assert sharing(client, parent_token) == [
        {
            "viewerUserId": child["id"],
            "viewerName": "김철수",
            "relation": "",
            "report": True,
            "timeline": True,
        }
    ]
    assert sharing(client, child_token) == [
        {
            "viewerUserId": parent["id"],
            "viewerName": "어머니",
            "relation": "MOTHER",
            "report": True,
            "timeline": True,
        }
    ]
    loner_token, _ = create_user(client, "01040000001", "PARENT", "혼자")
    assert sharing(client, loner_token) == []


def test_revoking_report_or_timeline_blocks_only_that_part(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    assert put_sharing(client, parent_token, child["id"], report=False, timeline=True) == 200
    assert sharing(client, parent_token)[0]["report"] is False
    assert sharing(client, parent_token)[0]["timeline"] is True
    for path in REPORT_PATHS:
        assert status_of(client, child_token, parent["id"], path) == 403
        assert status_of(client, parent_token, parent["id"], path) == 200
    for path in ("calls", "profile", "daily-questions"):
        assert status_of(client, child_token, parent["id"], path) == 200
    # The child still sees their own sharing untouched.
    assert sharing(client, child_token)[0]["report"] is True
    assert status_of(client, parent_token, child["id"], "reports?period=WEEKLY") == 200

    assert put_sharing(client, parent_token, child["id"], report=True, timeline=False) == 200
    assert status_of(client, child_token, parent["id"], "calls") == 403
    for path in REPORT_PATHS:
        assert status_of(client, child_token, parent["id"], path) == 200


def test_sharing_only_accepts_members_of_the_same_family(client: TestClient) -> None:
    _, _, parent_token, _ = onboard_family(client)
    _, stranger = create_user(client, "01040000002", "CHILD", "남")
    assert put_sharing(client, parent_token, stranger["id"], report=False, timeline=False) == 422


def test_member_removal_permissions(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    family_id = child["familyId"]
    father_token, father = create_user(client, "01040000003", "PARENT", "아버지")
    joined = accept(client, father_token, invite(client, child_token, family_id, "FATHER")["code"])
    assert joined.status_code == 200
    pending = invite(client, parent_token, family_id, "FATHER")

    creator_view = members(client, child_token, family_id)
    assert creator_view[child["id"]]["canRemove"] is False
    assert creator_view[parent["id"]]["canRemove"] is True
    member_view = members(client, parent_token, family_id)
    assert member_view[parent["id"]]["canRemove"] is True
    assert member_view[father["id"]]["canRemove"] is False
    assert member_view[child["id"]]["canRemove"] is False
    pending_row = next(row for row in member_view.values() if row["userId"] is None)
    assert pending_row["canRemove"] is True

    outsider_token, _ = create_user(client, "01040000004", "CHILD", "남")
    assert remove(client, outsider_token, family_id, pending_row["memberId"]) == 403
    assert remove(client, parent_token, family_id, "missing") == 404
    assert remove(client, parent_token, family_id, child["id"]) == 409
    other = creator_view[father["id"]]["memberId"]
    assert remove(client, parent_token, family_id, other) == 403

    assert remove(client, parent_token, family_id, pending_row["memberId"]) == 204
    assert accept(client, outsider_token, pending["code"]).status_code == 404

    assert remove(client, child_token, family_id, other) == 204
    removed = client.get(f"/v1/families/{family_id}/members", headers=auth(father_token))
    assert removed.status_code == 403
    assert login(client, "01040000003")["user"]["familyId"] is None
    call = client.post("/v1/calls", headers=auth(father_token), json={"calleeId": child["id"]})
    assert call.status_code == 403

    assert remove(client, parent_token, family_id, member_view[parent["id"]]["memberId"]) == 204
    assert set(members(client, child_token, family_id)) == {child["id"]}


def test_leaving_resets_sharing_between_the_two_people(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    assert put_sharing(client, parent_token, child["id"], report=False, timeline=False) == 200
    member_id = members(client, child_token, child["familyId"])[parent["id"]]["memberId"]
    assert remove(client, parent_token, child["familyId"], member_id) == 204
    assert sharing(client, parent_token) == []
    rejoined = accept(client, parent_token, invite(client, child_token, child["familyId"])["code"])
    assert rejoined.status_code == 200
    assert sharing(client, parent_token)[0]["report"] is True
    assert status_of(client, child_token, parent["id"], "reports?period=WEEKLY") == 200


def test_marketing_preference_and_account_deletion_clear_sharing(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    assert parent["marketingOptIn"] is False
    updated = client.patch(
        "/v1/account/preferences", headers=auth(parent_token), json={"marketingOptIn": True}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["marketingOptIn"] is True
    assert updated.json()["familyId"] == child["familyId"]
    assert login(client, "01033334444")["user"]["marketingOptIn"] is True
    empty = client.patch("/v1/account/preferences", headers=auth(parent_token), json={})
    assert empty.status_code == 422

    assert put_sharing(client, parent_token, child["id"], report=False, timeline=True) == 200
    assert put_sharing(client, child_token, parent["id"], report=True, timeline=False) == 200
    assert client.delete("/v1/account", headers=auth(parent_token)).status_code == 204

    async def remaining() -> int:
        async with client.app.state.container.database.sessions() as session:
            return await session.scalar(select(func.count()).select_from(FamilyShare))

    assert client.portal.call(remaining) == 0
