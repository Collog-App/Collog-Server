from fastapi.testclient import TestClient

from tests.conftest import auth, create_user
from tests.test_api_flow import onboard_family


def test_empty_profile_records_completed_setup(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    path = f"/v1/parents/{parent['id']}/profile"
    missing = client.get(path, headers=auth(parent_token))
    assert missing.status_code == 200
    assert missing.json()["isCompleted"] is False
    saved = client.put(path, headers=auth(parent_token), json={"conditions": []})
    assert saved.status_code == 200, saved.text
    assert saved.json()["isCompleted"] is True
    restored = client.get(path, headers=auth(child_token)).json()
    assert restored["isCompleted"] is True
    assert restored["conditions"] == []
    assert restored["updatedAt"] is not None
    questions = client.get(
        f"/v1/parents/{parent['id']}/daily-questions", headers=auth(parent_token)
    )
    assert questions.status_code == 200
    assert questions.json()["questions"]


def test_child_can_clear_selected_parent_profile(client: TestClient) -> None:
    child_token, _, parent_token, parent = onboard_family(client)
    path = f"/v1/parents/{parent['id']}/profile"
    created = client.put(path, headers=auth(parent_token), json={"conditions": ["ASTHMA"]})
    assert created.status_code == 200
    cleared = client.put(path, headers=auth(child_token), json={"conditions": []})
    assert cleared.status_code == 200
    assert cleared.json()["parentId"] == parent["id"]
    assert cleared.json()["conditions"] == []
    assert cleared.json()["isCompleted"] is True
    outsider_token, _ = create_user(client, "01099998888", "CHILD", "다른 가족")
    rejected = client.put(path, headers=auth(outsider_token), json={"conditions": ["DIABETES"]})
    assert rejected.status_code == 403
    assert client.get(path, headers=auth(parent_token)).json()["conditions"] == []
