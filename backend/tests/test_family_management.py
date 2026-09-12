from datetime import UTC, datetime, timedelta

from app.models import Invitation
from tests.conftest import auth, create_user


def setup_family(client):
    child_token, child = create_user(client, "01011112222", "CHILD", "Child")
    parent_token, parent = create_user(client, "01033334444", "PARENT", "Parent")
    url = f"/v1/families/{child['familyId']}"
    invitation = client.post(
        f"{url}/invitations",
        headers=auth(child_token),
        json={"name": "Parent", "relation": "MOTHER"},
    )
    assert invitation.status_code == 201
    return child_token, child, parent_token, parent, url, invitation.json()


def test_members_include_owner_and_keep_invitation_private(client):
    child_token, child, parent_token, parent, url, invitation = setup_family(client)
    owner_view = client.get(f"{url}/members", headers=auth(child_token)).json()
    assert owner_view["canInvite"] is True
    assert owner_view["members"][0]["invitation"] == invitation
    assert owner_view["members"][-1]["userId"] == child["id"]
    assert owner_view["members"][-1]["role"] == "CHILD"
    assert (
        client.post(
            "/v1/invitations/accept", headers=auth(parent_token), json={"code": invitation["code"]}
        ).status_code
        == 200
    )
    parent_view = client.get(f"{url}/members", headers=auth(parent_token)).json()
    assert parent_view["canInvite"] is False
    assert all(row["invitation"] is None for row in parent_view["members"])
    assert parent_view["members"][0]["userId"] == parent["id"]


def test_resend_invalidates_previous_code_and_exposes_latest(client):
    child_token, _, parent_token, _, url, invitation = setup_family(client)
    refreshed = client.post(
        f"/v1/invitations/{invitation['invitationId']}/resend", headers=auth(child_token)
    )
    assert refreshed.status_code == 201
    assert refreshed.json()["code"] != invitation["code"]
    assert (
        client.post(
            "/v1/invitations/accept", headers=auth(parent_token), json={"code": invitation["code"]}
        ).status_code
        == 410
    )
    members = client.get(f"{url}/members", headers=auth(child_token)).json()["members"]
    assert members[0]["invitation"] == refreshed.json()
    assert (
        client.post(
            "/v1/invitations/accept",
            headers=auth(parent_token),
            json={"code": refreshed.json()["code"]},
        ).status_code
        == 200
    )


def test_accepted_invitation_retry_and_conflicts(client):
    child_token, _, parent_token, _, url, invitation = setup_family(client)

    def accept(token, code):
        return client.post("/v1/invitations/accept", headers=auth(token), json={"code": code})

    assert accept(parent_token, invitation["code"]).status_code == 200
    assert accept(parent_token, invitation["code"]).status_code == 200
    other_token, _ = create_user(client, "01055556666", "PARENT", "Other")
    assert accept(other_token, invitation["code"]).status_code == 409
    assert (
        client.post(
            f"/v1/invitations/{invitation['invitationId']}/resend", headers=auth(child_token)
        ).status_code
        == 409
    )
    duplicate = client.post(
        f"{url}/invitations",
        headers=auth(child_token),
        json={"name": "Parent", "relation": "FATHER"},
    ).json()
    assert accept(parent_token, duplicate["code"]).status_code == 409


def test_accepted_membership_does_not_expire_with_invitation(client):
    child_token, _, parent_token, _, url, invitation = setup_family(client)
    response = client.post(
        "/v1/invitations/accept", headers=auth(parent_token), json={"code": invitation["code"]}
    )
    assert response.status_code == 200

    async def expire():
        async with client.app.state.container.database.sessions() as session:
            stored = await session.get(Invitation, invitation["invitationId"])
            stored.expires_at = datetime.now(UTC) - timedelta(days=1)
            await session.commit()

    client.portal.call(expire)
    members = client.get(f"{url}/members", headers=auth(child_token)).json()["members"]
    assert members[0]["status"] == "AWAITING_CONSENT"
    assert members[0]["invitation"]["status"] == "ACCEPTED"
    retry = client.post(
        "/v1/invitations/accept", headers=auth(parent_token), json={"code": invitation["code"]}
    )
    assert retry.status_code == 200
