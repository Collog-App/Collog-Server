from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import HealthDailySummary, ParentProfile, TodoItem
from app.services.reports import REPORT_TIMEZONE
from app.services.todos import (
    DEFAULT_POOL,
    LOW_STEPS_SUGGESTION,
    SUGGESTION_POOL,
    daily_suggestions,
)
from tests.conftest import auth, create_user


def todos(client: TestClient, token: str) -> list[dict]:
    response = client.get("/v1/todos", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def seed(client: TestClient, *rows) -> None:
    async def run() -> None:
        async with client.app.state.container.database.sessions() as session:
            session.add_all(rows)
            await session.commit()

    client.portal.call(run)


def stored(client: TestClient, user_id: str) -> list[TodoItem]:
    async def run() -> list[TodoItem]:
        async with client.app.state.container.database.sessions() as session:
            return list(await session.scalars(select(TodoItem).where(TodoItem.user_id == user_id)))

    return client.portal.call(run)


def test_first_open_makes_three_suggestions_once(client: TestClient) -> None:
    token, user = create_user(client, "01011112222", "PARENT", "엄마")

    first = todos(client, token)
    assert len(first) == 3
    assert {item["source"] for item in first} == {"SUGGESTED"}
    assert all(item["done"] is False for item in first)
    default_titles = {text for _, text in DEFAULT_POOL}
    assert {item["title"] for item in first} <= default_titles

    # 다시 열어도 같은 세 개이고 새로 만들지 않는다
    assert todos(client, token) == first
    assert len(stored(client, user["id"])) == 3


def test_suggestions_follow_conditions_and_low_steps(client: TestClient) -> None:
    token, user = create_user(client, "01011113333", "PARENT", "아빠")
    yesterday = datetime.now(REPORT_TIMEZONE).date() - timedelta(days=1)
    seed(
        client,
        ParentProfile(parent_id=user["id"], conditions=["HYPERTENSION"]),
        HealthDailySummary(
            user_id=user["id"], day=yesterday, platform="HEALTH_CONNECT", steps=1200,
            updated_at=datetime.now(UTC),
        ),
    )

    titles = [item["title"] for item in todos(client, token)]
    assert titles[0] == LOW_STEPS_SUGGESTION[1]
    hypertension = {text for _, text in SUGGESTION_POOL["HYPERTENSION"]}
    assert set(titles[1:]) == hypertension


def test_daily_suggestions_are_stable_and_unique() -> None:
    today = datetime.now(REPORT_TIMEZONE).date()
    picked = daily_suggestions("user-1", today, ["OBESITY", "DIABETES"], None)
    assert picked == daily_suggestions("user-1", today, ["OBESITY", "DIABETES"], None)
    assert len(picked) == 3
    assert len({text for _, text in picked}) == 3
    # 걸음 수가 충분하면 걷기 추천을 앞에 두지 않는다
    assert LOW_STEPS_SUGGESTION not in daily_suggestions("user-1", today, [], 8000)


def test_own_todo_create_complete_delete(client: TestClient) -> None:
    token, _ = create_user(client, "01011114444", "CHILD", "딸")

    created = client.post("/v1/todos", headers=auth(token), json={"title": "  약국 들르기  "})
    assert created.status_code == 201, created.text
    todo = created.json()
    assert todo["title"] == "약국 들르기"
    assert todo["source"] == "USER"
    assert todo["done"] is False

    items = todos(client, token)
    assert [item["source"] for item in items] == ["SUGGESTED"] * 3 + ["USER"]

    done = client.patch(f"/v1/todos/{todo['id']}", headers=auth(token), json={"done": True})
    assert done.status_code == 200, done.text
    assert done.json()["done"] is True
    # 끝낸 할 일도 그날은 목록에 남는다
    assert todos(client, token)[-1]["done"] is True

    undone = client.patch(f"/v1/todos/{todo['id']}", headers=auth(token), json={"done": False})
    assert undone.json()["done"] is False

    assert client.delete(f"/v1/todos/{todo['id']}", headers=auth(token)).status_code == 204
    assert [item["source"] for item in todos(client, token)] == ["SUGGESTED"] * 3


def test_deleted_suggestion_stays_hidden_that_day(client: TestClient) -> None:
    token, user = create_user(client, "01011115555", "PARENT", "할머니")
    first = todos(client, token)

    hidden = first[0]["id"]
    assert client.delete(f"/v1/todos/{hidden}", headers=auth(token)).status_code == 204
    after = todos(client, token)
    assert [item["id"] for item in after] == [item["id"] for item in first[1:]]
    # 숨긴 추천은 다시 만들지 않고, 완료 표시도 할 수 없다
    assert len(stored(client, user["id"])) == 3
    blocked = client.patch(f"/v1/todos/{hidden}", headers=auth(token), json={"done": True})
    assert blocked.status_code == 404


def test_todos_are_private(client: TestClient) -> None:
    owner_token, _ = create_user(client, "01011116666", "PARENT", "주인")
    other_token, _ = create_user(client, "01011117777", "CHILD", "남")
    todo = client.post("/v1/todos", headers=auth(owner_token), json={"title": "산책"}).json()

    assert all(item["id"] != todo["id"] for item in todos(client, other_token))
    path = f"/v1/todos/{todo['id']}"
    assert client.patch(path, headers=auth(other_token), json={"done": True}).status_code == 404
    assert client.delete(path, headers=auth(other_token)).status_code == 404
    assert client.get("/v1/todos").status_code == 401


def test_title_is_required_and_short(client: TestClient) -> None:
    token, _ = create_user(client, "01011118888", "CHILD", "아들")
    assert client.post("/v1/todos", headers=auth(token), json={"title": "   "}).status_code == 422
    too_long = client.post("/v1/todos", headers=auth(token), json={"title": "가" * 101})
    assert too_long.status_code == 422


def test_account_deletion_removes_todos(client: TestClient) -> None:
    token, user = create_user(client, "01011119999", "CHILD", "지움")
    client.post("/v1/todos", headers=auth(token), json={"title": "지울 할 일"})
    todos(client, token)
    assert stored(client, user["id"])

    assert client.delete("/v1/account", headers=auth(token)).status_code == 204
    assert stored(client, user["id"]) == []
