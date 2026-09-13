from fastapi.testclient import TestClient

from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family
from tests.test_auth import login


def join_family(client: TestClient, parent_token: str, child_token: str, family_id: str) -> None:
    invitation = client.post(
        f"/v1/families/{family_id}/invitations",
        headers=auth(child_token), json={"name": "부모", "relation": "MOTHER"},
    )
    assert invitation.status_code == 201
    accepted = client.post(
        "/v1/invitations/accept", headers=auth(parent_token),
        json={"code": invitation.json()["code"]},
    )
    assert accepted.status_code == 200
    assert accepted.json()["familyId"] == family_id


def test_parent_role_change_ignores_empty_owned_family(client: TestClient) -> None:
    token, original = create_user(client, "01012345678", "CHILD", "사용자")
    changed = client.patch("/v1/account/role", headers=auth(token), json={"role": "PARENT"})
    assert changed.status_code == 200
    assert changed.json()["familyId"] is None
    child_token, child = create_user(client, "01099998888", "CHILD", "자녀")
    join_family(client, token, child_token, child["familyId"])
    signed_in = login(client)
    assert signed_in["user"]["role"] == "PARENT"
    assert signed_in["user"]["familyId"] == child["familyId"]
    assert signed_in["user"]["familyId"] != original["familyId"]


def test_parent_uses_most_recently_accepted_family_after_signing_in(client: TestClient) -> None:
    _, old_child, parent_token, _ = onboard_family(client)
    child_token, child = create_user(client, "01099998888", "CHILD", "다른 자녀")
    join_family(client, parent_token, child_token, child["familyId"])
    signed_in = login(client, "01033334444")
    assert signed_in["user"]["familyId"] == child["familyId"]
    assert signed_in["user"]["familyId"] != old_child["familyId"]
