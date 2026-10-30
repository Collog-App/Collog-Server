from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import CallRecord, Family
from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_auth import login


def grant(client: TestClient, token: str) -> None:
    document = client.get("/v1/consents/document").json()
    response = client.post(
        "/v1/consents",
        headers=auth(token),
        json={
            "documentVersion": document["version"],
            "decision": "GRANT",
            "scrolledToEnd": True,
            "agreedItems": document["requiredItems"],
        },
    )
    assert response.status_code == 201, response.text


def invite(client: TestClient, token: str, family_id: str, relation: str = "MOTHER") -> dict:
    response = client.post(
        f"/v1/families/{family_id}/invitations",
        headers=auth(token),
        json={"name": "가족", "relation": relation},
    )
    assert response.status_code == 201, response.text
    return response.json()


def accept(client: TestClient, token: str, code: str):
    return client.post("/v1/invitations/accept", headers=auth(token), json={"code": code})


def stored_call(client: TestClient, call_id: str) -> CallRecord:
    async def load() -> CallRecord:
        async with client.app.state.container.database.sessions() as session:
            return await session.get(CallRecord, call_id)

    return client.portal.call(load)


def siblings(client: TestClient):
    first_token, first = create_user(client, "01020000001", "CHILD", "첫째")
    second_token, second = create_user(client, "01020000002", "CHILD", "둘째")
    assert second["familyId"] is not None
    invitation = invite(client, first_token, first["familyId"])
    joined = accept(client, second_token, invitation["code"])
    assert joined.status_code == 200, joined.text
    assert joined.json()["familyId"] == first["familyId"]
    for token in (first_token, second_token):
        grant(client, token)
    return first_token, first, second_token, second


def test_siblings_can_call_each_other_and_the_callee_is_the_subject(client: TestClient) -> None:
    first_token, first, second_token, second = siblings(client)
    created = client.post("/v1/calls", headers=auth(second_token), json={"calleeId": first["id"]})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["callerId"] == second["id"]
    assert body["calleeId"] == first["id"]
    assert body["recordingEnabled"] is True
    assert body["rawCaptureRequired"] is False
    call = stored_call(client, body["callId"])
    assert (call.parent_id, call.child_id) == (first["id"], second["id"])
    assert call.caller_id == second["id"]
    accepted = client.post(f"/v1/calls/{body['callId']}/accept", headers=auth(first_token))
    assert accepted.status_code == 200
    assert accepted.json()["rawCaptureRequired"] is True
    view = client.get(f"/v1/calls/{body['callId']}", headers=auth(second_token)).json()
    assert (view["callerId"], view["calleeId"]) == (second["id"], first["id"])


def test_parent_stays_the_subject_in_both_directions(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    for token, callee in ((parent_token, child["id"]), (child_token, parent["id"])):
        created = client.post("/v1/calls", headers=auth(token), json={"calleeId": callee})
        assert created.status_code == 201, created.text
        call = stored_call(client, created.json()["callId"])
        assert (call.parent_id, call.child_id) == (parent["id"], child["id"])
        assert created.json()["calleeId"] == callee
        ended = client.post(f"/v1/calls/{call.id}/end", headers=auth(token))
        assert ended.status_code == 200


def test_accepting_dissolves_a_family_with_only_yourself(client: TestClient) -> None:
    host_token, host = create_user(client, "01020000003", "CHILD", "초대한 사람")
    guest_token, guest = create_user(client, "01020000004", "CHILD", "초대받은 사람")
    pending = invite(client, guest_token, guest["familyId"])
    invitation = invite(client, host_token, host["familyId"])
    accepted = accept(client, guest_token, invitation["code"])
    assert accepted.status_code == 200, accepted.text

    async def family_ids() -> set[str]:
        async with client.app.state.container.database.sessions() as session:
            return set(await session.scalars(select(Family.id)))

    assert client.portal.call(family_ids) == {host["familyId"]}
    old = client.get(f"/v1/families/{guest['familyId']}/members", headers=auth(guest_token))
    assert old.status_code == 403
    stranger_token, _ = create_user(client, "01020000005", "PARENT", "다른 사람")
    assert accept(client, stranger_token, pending["code"]).status_code == 404
    assert login(client, "01020000004")["user"]["familyId"] == host["familyId"]
    members = client.get(f"/v1/families/{host['familyId']}/members", headers=auth(host_token))
    assert {row["userId"] for row in members.json()["members"]} == {host["id"], guest["id"]}


def test_creator_of_a_populated_family_cannot_join_another(client: TestClient) -> None:
    child_token, child, _, _ = onboard_family(client)
    other_token, other = create_user(client, "01020000006", "CHILD", "다른 자녀")
    invitation = invite(client, other_token, other["familyId"])
    rejected = accept(client, child_token, invitation["code"])
    assert rejected.status_code == 409
    own = invite(client, child_token, child["familyId"])
    assert accept(client, child_token, own["code"]).status_code == 409


def test_any_member_can_invite_and_post_families_returns_or_creates(client: TestClient) -> None:
    _, child, parent_token, _ = onboard_family(client)
    invitation = invite(client, parent_token, child["familyId"], "FATHER")
    assert invitation["status"] == "PENDING"
    resent = client.post(
        f"/v1/invitations/{invitation['invitationId']}/resend", headers=auth(parent_token)
    )
    assert resent.status_code == 201
    existing = client.post("/v1/families", headers=auth(parent_token))
    assert existing.status_code == 200
    assert existing.json() == {"familyId": child["familyId"]}

    loner_token, loner = create_user(client, "01020000007", "PARENT", "혼자")
    assert loner["familyId"] is None
    created = client.post("/v1/families", headers=auth(loner_token))
    assert created.status_code == 201
    again = client.post("/v1/families", headers=auth(loner_token))
    assert again.status_code == 200
    assert again.json() == created.json()
    assert invite(client, loner_token, created.json()["familyId"])["status"] == "PENDING"
    outsider = client.post(
        f"/v1/families/{child['familyId']}/invitations",
        headers=auth(loner_token),
        json={"name": "가족", "relation": "MOTHER"},
    )
    assert outsider.status_code == 403


def test_everyone_owns_a_profile_that_family_members_can_edit(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    own = f"/v1/parents/{child['id']}/profile"
    assert client.get(own, headers=auth(child_token)).status_code == 200
    saved = client.put(own, headers=auth(child_token), json={"conditions": ["RHINITIS"]})
    assert saved.status_code == 200, saved.text
    edited = client.put(own, headers=auth(parent_token), json={"conditions": ["ASTHMA"]})
    assert edited.status_code == 200
    assert client.get(own, headers=auth(child_token)).json()["conditions"] == ["ASTHMA"]
    outsider_token, _ = create_user(client, "01020000008", "PARENT", "남")
    for path in ("daily-questions", "reports?period=WEEKLY", "calls", "signals", "baseline"):
        for subject in (child["id"], parent["id"]):
            viewed = client.get(f"/v1/parents/{subject}/{path}", headers=auth(parent_token))
            assert viewed.status_code == 200, (path, viewed.text)
        hidden = client.get(f"/v1/parents/{child['id']}/{path}", headers=auth(outsider_token))
        assert hidden.status_code == 403
    assert client.get(own, headers=auth(outsider_token)).status_code == 403


def test_profile_needs_the_owners_consent(client: TestClient) -> None:
    child_token, child = create_user(client, "01020000009", "CHILD", "자녀")
    blocked = client.put(
        f"/v1/parents/{child['id']}/profile", headers=auth(child_token), json={"conditions": []}
    )
    assert blocked.status_code == 409


def test_ios_login_with_a_role_keeps_working(client: TestClient) -> None:
    child_token, child = create_user(client, "01020000010", "CHILD", "자녀")
    parent_token, parent = create_user(client, "01020000011", "PARENT", "부모")
    assert child["role"] == "CHILD"
    assert child["familyId"] is not None
    assert parent["role"] == "PARENT"
    assert parent["familyId"] is None
    invitation = invite(client, child_token, child["familyId"])
    assert accept(client, parent_token, invitation["code"]).status_code == 200
    assert login(client, "01020000011")["user"]["familyId"] == child["familyId"]
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201

    async def count() -> int:
        async with client.app.state.container.database.sessions() as session:
            return await session.scalar(select(func.count(Family.id)))

    assert client.portal.call(count) == 1


def test_invitations_accept_any_family_relation(client: TestClient) -> None:
    _, child, parent_token, _ = onboard_family(client)
    for relation in ("SPOUSE", "CHILD", "SIBLING", "GRANDPARENT", "OTHER"):
        assert invite(client, parent_token, child["familyId"], relation)["status"] == "PENDING"
    unknown = client.post(
        f"/v1/families/{child['familyId']}/invitations",
        headers=auth(parent_token),
        json={"name": "가족", "relation": "COUSIN"},
    )
    assert unknown.status_code == 422
