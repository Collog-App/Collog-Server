from fastapi.testclient import TestClient

from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family

PROFILE = {"nickname": "콜록이1", "gender": "FEMALE", "birthDate": "1958-03-14"}


def save(client: TestClient, token: str, body: dict | None = None):
    return client.patch("/v1/account/profile", headers=auth(token), json=PROFILE | (body or {}))


def available(client: TestClient, token: str, nickname: str) -> dict:
    response = client.get(
        "/v1/account/nickname-availability", headers=auth(token), params={"nickname": nickname}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_nickname_availability_reports_invalid_and_taken(client: TestClient) -> None:
    first_token, _ = create_user(client, "01030000001", "CHILD", "첫째")
    second_token, _ = create_user(client, "01030000002", "CHILD", "둘째")
    for nickname in ("a", "가" * 11, "닉 네임", "닉네임!", "ㄱㄴㄷ"):
        assert available(client, first_token, nickname) == {"available": False, "reason": "INVALID"}
    assert available(client, first_token, "콜록이1") == {"available": True, "reason": None}
    assert save(client, first_token).status_code == 200
    assert available(client, second_token, "콜록이1") == {"available": False, "reason": "TAKEN"}
    assert available(client, first_token, "콜록이1") == {"available": True, "reason": None}
    anonymous = client.get("/v1/account/nickname-availability", params={"nickname": "abc"})
    assert anonymous.status_code == 401


def test_profile_update_returns_the_user_and_rejects_duplicates(client: TestClient) -> None:
    first_token, first = create_user(client, "01030000003", "CHILD", "첫째")
    second_token, _ = create_user(client, "01030000004", "PARENT", "둘째")
    for key in ("nickname", "gender", "birthDate", "email"):
        assert first[key] is None
    assert first["signupCompleted"] is False

    saved = save(client, first_token)
    assert saved.status_code == 200, saved.text
    user = saved.json()
    assert [user[key] for key in PROFILE] == list(PROFILE.values())
    assert user["signupCompleted"] is True
    assert user["familyId"] == first["familyId"]
    assert user["role"] == "CHILD"

    assert save(client, second_token).status_code == 409
    again = save(client, first_token, {"gender": "MALE"})
    assert again.status_code == 200
    assert again.json()["gender"] == "MALE"


def test_profile_update_validates_every_field(client: TestClient) -> None:
    token, _ = create_user(client, "01030000005", "CHILD", "사용자")
    for change in (
        {"nickname": "a"},
        {"nickname": "닉네임!"},
        {"gender": "OTHER"},
        {"birthDate": "2999-01-01"},
        {"birthDate": "1850-01-01"},
        {"birthDate": "not-a-date"},
    ):
        assert save(client, token, change).status_code == 422, change
    for missing in PROFILE:
        body = {key: value for key, value in PROFILE.items() if key != missing}
        response = client.patch("/v1/account/profile", headers=auth(token), json=body)
        assert response.status_code == 422, missing


def test_member_list_shows_nickname_and_birth_date(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    assert save(client, parent_token).status_code == 200
    members = client.get(
        f"/v1/families/{child['familyId']}/members", headers=auth(child_token)
    ).json()["members"]
    rows = {row["userId"]: row for row in members}
    assert rows[parent["id"]]["nickname"] == PROFILE["nickname"]
    assert rows[parent["id"]]["birthDate"] == PROFILE["birthDate"]
    assert (rows[child["id"]]["nickname"], rows[child["id"]]["birthDate"]) == (None, None)
