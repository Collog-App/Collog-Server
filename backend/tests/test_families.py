from fastapi.testclient import TestClient

from tests.conftest import auth, create_user


def test_parent_can_list_and_revisit_multiple_families(client: TestClient) -> None:
    first_token, first = create_user(client, "01011110001", "CHILD", "첫째")
    second_token, second = create_user(client, "01011110002", "CHILD", "둘째")
    _, stranger = create_user(client, "01011110003", "CHILD", "다른 가족")
    parent_token, _ = create_user(client, "01011110004", "PARENT", "부모")
    assert client.get("/v1/families", headers=auth(parent_token)).json() == {"families": []}

    for child_token, child in [(first_token, first), (second_token, second)]:
        invitation = client.post(
            f"/v1/families/{child['familyId']}/invitations",
            headers=auth(child_token),
            json={"name": "부모", "relation": "MOTHER"},
        )
        assert invitation.status_code == 201
        accepted = client.post(
            "/v1/invitations/accept",
            headers=auth(parent_token),
            json={"code": invitation.json()["code"]},
        )
        assert accepted.status_code == 200

    response = client.get("/v1/families", headers=auth(parent_token))
    assert response.status_code == 200
    assert response.json()["families"] == [
        {"familyId": first["familyId"], "name": "첫째의 가족"},
        {"familyId": second["familyId"], "name": "둘째의 가족"},
    ]
    for child in [first, second]:
        members = client.get(
            f"/v1/families/{child['familyId']}/members", headers=auth(parent_token)
        )
        assert members.status_code == 200
        assert any(member["userId"] == child["id"] for member in members.json()["members"])
    forbidden = client.get(
        f"/v1/families/{stranger['familyId']}/members", headers=auth(parent_token)
    )
    assert forbidden.status_code == 403


def test_family_list_is_private_and_includes_owned_family(client: TestClient) -> None:
    token, child = create_user(client, "01011110005", "CHILD", "자녀")
    create_user(client, "01011110006", "CHILD", "다른 자녀")
    assert client.get("/v1/families").status_code == 401
    response = client.get("/v1/families", headers=auth(token))
    assert response.status_code == 200
    assert response.json() == {"families": [{"familyId": child["familyId"], "name": "자녀의 가족"}]}


def test_parent_role_change_does_not_restore_empty_owned_family(client: TestClient) -> None:
    token, _ = create_user(client, "01011110007", "CHILD", "부모")
    changed = client.patch("/v1/account/role", headers=auth(token), json={"role": "PARENT"})
    assert changed.status_code == 200
    response = client.get("/v1/families", headers=auth(token))
    assert response.status_code == 200
    assert response.json() == {"families": []}
