from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.config import Settings
from app.models import (
    AnalysisUsage,
    AssetKind,
    AssetStatus,
    AudioAsset,
    CallRecord,
    FamilyShare,
    RevenueCatEvent,
    User,
    UserEntitlement,
)
from app.services import billing
from app.services.revenuecat import (
    LIFETIME,
    EntitlementState,
    RevenueCatClient,
    RevenueCatError,
    entitlement_from_subscriber,
    related_user_ids,
)
from tests.conftest import auth
from tests.test_api_flow import onboard_family, tone_wav

KST = ZoneInfo("Asia/Seoul")
LONG_TALK = (
    "PARENT: 어제는 잠을 푹 잤고 아침도 챙겨 먹었어. 오후에는 공원을 삼십 분 정도 "
    "천천히 산책했고 저녁에는 친구랑 통화도 했어. 다른 불편함은 없어."
)
WEBHOOK_AUTH = "Bearer test-webhook-value"


def settings_of(client: TestClient) -> Settings:
    return client.app.state.container.settings


def run_db(client: TestClient, work):
    async def runner():
        async with client.app.state.container.database.sessions() as session:
            result = await work(session)
            await session.commit()
            return result

    return client.portal.call(runner)


def enable_billing(client: TestClient, launch: datetime) -> None:
    settings_of(client).billing_launch_at = launch


def set_joined(client: TestClient, user_ids: list[str], when: datetime) -> None:
    async def work(session):
        await session.execute(update(User).where(User.id.in_(user_ids)).values(created_at=when))

    run_db(client, work)


def end_trial(client: TestClient, *user_ids: str) -> None:
    """결제는 20일 전에 켜졌고 모두 30일 전에 가입해 체험이 6일 전에 끝났다."""
    now = datetime.now(UTC)
    enable_billing(client, now - timedelta(days=20))
    set_joined(client, list(user_ids), now - timedelta(days=30))


def add_usage(
    client: TestClient, seconds: int, parent_id: str | None, child_id: str | None,
    month: str | None = None, call_id: str | None = None, created_at: datetime | None = None,
) -> str:
    call_id = call_id or f"old-call-{seconds}-{parent_id}"

    async def work(session):
        session.add(AnalysisUsage(
            call_id=call_id, parent_id=parent_id, child_id=child_id,
            month=month or billing.kst_month(datetime.now(UTC)), seconds=seconds,
            created_at=created_at or datetime.now(UTC),
        ))

    run_db(client, work)
    return call_id


def grant_paid(client: TestClient, user_id: str, until: datetime) -> None:
    async def work(session):
        session.add(UserEntitlement(user_id=user_id, active_until=until, store="PLAY_STORE"))

    run_db(client, work)


def entitlement(client: TestClient, token: str) -> dict:
    response = client.get("/v1/account/entitlement", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def report(client: TestClient, token: str, parent_id: str):
    return client.get(f"/v1/parents/{parent_id}/reports?period=WEEKLY", headers=auth(token))


def usage_rows(client: TestClient, call_id: str | None = None) -> list[AnalysisUsage]:
    async def work(session):
        statement = select(AnalysisUsage)
        if call_id:
            statement = statement.where(AnalysisUsage.call_id == call_id)
        return list(await session.scalars(statement))

    return run_db(client, work)


def count_provider_calls(client: TestClient) -> dict[str, int]:
    """mock STT·Gemini 를 부른 횟수를 센다(인스턴스 속성으로 덮어 파이프라인이 그대로 쓴다)."""
    container = client.app.state.container
    counts = {"stt": 0, "gemini": 0}

    def counting(target, name: str, key: str) -> None:
        original = getattr(target, name)

        async def wrapper(*args, **kwargs):
            counts[key] += 1
            return await original(*args, **kwargs)

        setattr(target, name, wrapper)

    counting(container.stt, "transcribe", "stt")
    counting(container.extraction, "extract", "gemini")
    counting(container.extraction, "summarize", "gemini")
    return counts


def run_call(
    client: TestClient, child_token: str, parent_token: str, parent: dict,
    parent_text: str = LONG_TALK, talk_seconds: int = 0,
) -> str:
    """test_guardrail.analyze_family_call 흐름. talk_seconds 만큼 통화한 것으로 받은 시각을 당긴다.

    통화가 끝나자마자 분석에서 빠지면(녹음 꺼짐) 기기 업로드는 건너뛰고, 늦게 끝난 녹음
    (egress_ended)만 보낸다.
    """
    created = client.post("/v1/calls", headers=auth(child_token), json={"calleeId": parent["id"]})
    assert created.status_code == 201, created.text
    call_id = created.json()["callId"]
    assert client.post(f"/v1/calls/{call_id}/accept", headers=auth(parent_token)).status_code == 200
    if talk_seconds:
        async def shift(session):
            await session.execute(
                update(CallRecord).where(CallRecord.id == call_id).values(
                    accepted_at=datetime.now(UTC) - timedelta(seconds=talk_seconds)
                )
            )

        run_db(client, shift)
    ended = client.post(f"/v1/calls/{call_id}/end", headers=auth(child_token))
    assert ended.status_code == 200, ended.text
    if client.get(f"/v1/calls/{call_id}", headers=auth(child_token)).json()["recordingEnabled"]:
        upload = client.post(
            f"/v1/calls/{call_id}/raw-audio/upload-url",
            headers=auth(parent_token),
            json={"contentType": "audio/wav", "durationSec": 6, "sampleRate": 16000},
        )
        assert upload.status_code == 200, upload.text
        parsed = urlsplit(upload.json()["uploadUrl"])
        assert client.put(parsed.path + "?" + parsed.query, content=tone_wav()).status_code == 204
        completed = client.post(
            f"/v1/calls/{call_id}/raw-audio/complete",
            headers=auth(parent_token),
            json={"assetId": upload.json()["assetId"]},
        )
        assert completed.status_code == 202, completed.text

    async def write_egress(session):
        assets = list(await session.scalars(select(AudioAsset).where(
            AudioAsset.call_id == call_id, AudioAsset.egress_id.is_not(None)
        )))
        storage = client.app.state.container.storage
        for asset in assets:
            parent_track = asset.kind == AssetKind.WEBRTC_EGRESS_PARENT.value
            text = parent_text if parent_track else "CHILD: 네"
            await storage.write(asset.uri.removeprefix("local://"), text.encode())
        return [asset.egress_id for asset in assets]

    for egress_id in run_db(client, write_egress):
        webhook = client.post(
            "/v1/webhooks/livekit",
            json={"event": "egress_ended",
                  "egress_info": {"egress_id": egress_id, "status": "EGRESS_COMPLETE"}},
        )
        assert webhook.status_code == 204, webhook.text
    return call_id


def call_view(client: TestClient, token: str, call_id: str) -> dict:
    return client.get(f"/v1/calls/{call_id}", headers=auth(token)).json()


# --- 결제 꺼짐 -----------------------------------------------------------------------------


def test_billing_off_keeps_everything_open_but_still_records_usage(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    # 꺼져 있으면 무료 한도를 넘게 써도, 체험이 없어도 그대로 분석·리포트가 열린다.
    set_joined(client, [child["id"], parent["id"]], datetime.now(UTC) - timedelta(days=90))
    add_usage(client, 99_999, parent["id"], child["id"])
    call_id = run_call(client, child_token, parent_token, parent, talk_seconds=90)

    assert call_view(client, child_token, call_id)["state"] == "ANALYZED"
    [row] = usage_rows(client, call_id)
    assert (row.parent_id, row.child_id) == (parent["id"], child["id"])
    assert 89 <= row.seconds <= 91
    assert report(client, child_token, parent["id"]).status_code == 200
    body = entitlement(client, parent_token)
    assert body["billingEnabled"] is False
    assert (body["plan"], body["source"], body["trialEndsAt"]) == ("PREMIUM", None, None)
    assert (body["paidByMe"], body["payerName"], body["paidUntil"]) == (False, None, None)
    assert body["willRenew"] is None
    assert body["analysis"]["limitSec"] == 15000
    assert body["analysis"]["usedSec"] == 99_999 + row.seconds


def test_launch_time_in_the_future_is_still_off(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)
    enable_billing(client, datetime.now(UTC) + timedelta(days=3))
    set_joined(client, [child["id"], parent["id"]], datetime.now(UTC) - timedelta(days=90))
    assert entitlement(client, child_token)["billingEnabled"] is False
    assert report(client, child_token, parent["id"]).status_code == 200


# --- 체험·가족 판정 ------------------------------------------------------------------------


def test_new_family_is_premium_during_trial(client: TestClient) -> None:
    enable_billing(client, datetime.now(UTC) - timedelta(days=1))
    child_token, _, parent_token, parent = onboard_family(client)
    body = entitlement(client, parent_token)
    assert (body["billingEnabled"], body["plan"], body["source"]) == (True, "PREMIUM", "TRIAL")
    trial_end = datetime.fromisoformat(body["trialEndsAt"])
    assert abs(trial_end - (datetime.now(UTC) + timedelta(days=14))) < timedelta(minutes=1)
    assert body["analysis"]["limitSec"] == 15000
    assert report(client, child_token, parent["id"]).status_code == 200


def test_trial_over_locks_reports_and_signals_with_402(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])

    body = entitlement(client, child_token)
    assert (body["plan"], body["source"]) == ("FREE", None)
    assert datetime.fromisoformat(body["trialEndsAt"]) < datetime.now(UTC)
    assert body["analysis"]["limitSec"] == 2400
    locked = report(client, child_token, parent["id"])
    assert locked.status_code == 402
    assert locked.json() == {
        "code": "PREMIUM_REQUIRED", "message": "주간 리포트는 프리미엄에서 볼 수 있어요.",
    }
    signals = client.get(f"/v1/parents/{parent['id']}/signals", headers=auth(child_token))
    assert signals.status_code == 402
    assert signals.json()["code"] == "PREMIUM_REQUIRED"
    # 무료 화면도 쓰는 API 는 막지 않는다.
    for path in (f"/v1/parents/{parent['id']}/baseline", f"/v1/parents/{parent['id']}/calls"):
        assert client.get(path, headers=auth(child_token)).status_code == 200


def test_share_off_stays_403_and_differs_from_premium_402(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)
    enable_billing(client, datetime.now(UTC) - timedelta(days=1))

    async def hide_report(session):
        session.add(FamilyShare(owner_id=parent["id"], viewer_id=child["id"], report=False))

    run_db(client, hide_report)
    hidden = report(client, child_token, parent["id"])
    assert hidden.status_code == 403
    assert hidden.json()["code"] == "SCOPE_DENIED"


def test_existing_members_get_two_weeks_from_launch(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)
    launch = datetime.now(UTC).replace(microsecond=0) - timedelta(days=3)
    enable_billing(client, launch)
    set_joined(client, [child["id"], parent["id"]], datetime.now(UTC) - timedelta(days=100))
    body = entitlement(client, child_token)
    assert (body["plan"], body["source"]) == ("PREMIUM", "TRIAL")
    assert datetime.fromisoformat(body["trialEndsAt"]) == launch + timedelta(days=14)


def test_new_member_does_not_extend_the_family_trial(client: TestClient) -> None:
    launch = datetime.now(UTC).replace(microsecond=0) - timedelta(days=20)
    enable_billing(client, launch)
    # 아이가 30일 전에 가입해 체험이 끝난 가족에 부모가 지금 들어온다.
    child_token, child, parent_token, parent = onboard_family(client)
    set_joined(client, [child["id"]], datetime.now(UTC) - timedelta(days=30))
    for token in (child_token, parent_token):
        body = entitlement(client, token)
        assert (body["plan"], body["source"]) == ("FREE", None)
        assert datetime.fromisoformat(body["trialEndsAt"]) == launch + timedelta(days=14)
    assert report(client, parent_token, parent["id"]).status_code == 402


def test_one_paying_member_makes_the_whole_family_premium(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    until = datetime.now(UTC).replace(microsecond=0) + timedelta(days=20)
    grant_paid(client, child["id"], until)

    mine = entitlement(client, child_token)
    assert (mine["plan"], mine["source"], mine["paidByMe"], mine["payerName"]) == (
        "PREMIUM", "PAID", True, None,
    )
    assert datetime.fromisoformat(mine["paidUntil"]) == until
    theirs = entitlement(client, parent_token)
    assert (theirs["plan"], theirs["source"], theirs["paidByMe"]) == ("PREMIUM", "PAID", False)
    assert theirs["payerName"] == "김철수"
    assert theirs["analysis"]["limitSec"] == 15000
    assert report(client, parent_token, parent["id"]).status_code == 200


def test_expired_subscription_no_longer_counts(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    grant_paid(client, child["id"], datetime.now(UTC) - timedelta(minutes=1))
    body = entitlement(client, child_token)
    assert (body["plan"], body["paidUntil"], body["paidByMe"]) == ("FREE", None, False)


def test_paid_wins_over_trial(client: TestClient) -> None:
    enable_billing(client, datetime.now(UTC) - timedelta(days=1))
    child_token, child, _, _ = onboard_family(client)
    grant_paid(client, child["id"], datetime.now(UTC) + timedelta(days=30))
    assert entitlement(client, child_token)["source"] == "PAID"


# --- 분석 분량 -----------------------------------------------------------------------------


def test_free_quota_used_up_skips_stt_and_gemini_and_deletes_audio(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    add_usage(client, 2400, parent["id"], child["id"])
    counts = count_provider_calls(client)

    call_id = run_call(client, child_token, parent_token, parent, talk_seconds=60)

    view = call_view(client, child_token, call_id)
    assert view["state"] == "ANALYSIS_EXCLUDED"
    assert view["recordingEnabled"] is False
    assert view["recordingDisabledReason"] == "ANALYSIS_QUOTA_EXCEEDED"
    assert view["recordingDisabledMessage"] == (
        "이번 달 분석 시간을 다 써서 이 통화는 분석하지 않았어요."
    )
    assert counts == {"stt": 0, "gemini": 0}
    assert usage_rows(client, call_id) == []

    # 통화 뒤에 늦게 끝난 녹음까지 지워진다.
    async def leftover(session):
        assets = await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call_id))
        storage = client.app.state.container.storage
        return [(asset.status, await storage.exists(asset.uri)) for asset in assets]

    remaining = run_db(client, leftover)
    assert remaining
    assert all(item == (AssetStatus.PURGED.value, False) for item in remaining)
    assert entitlement(client, child_token)["analysis"]["usedSec"] == 2400


def test_usage_by_another_family_does_not_count(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    add_usage(client, 5000, "someone-else", "another-child")
    assert entitlement(client, child_token)["analysis"]["usedSec"] == 0
    call_id = run_call(client, child_token, parent_token, parent)
    assert call_view(client, child_token, call_id)["state"] == "ANALYZED"


def test_call_longer_than_the_remaining_quota_is_still_analyzed(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    add_usage(client, 2300, parent["id"], None)

    first = run_call(client, child_token, parent_token, parent, talk_seconds=600)
    assert call_view(client, child_token, first)["state"] == "ANALYZED"
    [row] = usage_rows(client, first)
    assert 599 <= row.seconds <= 601
    used = entitlement(client, parent_token)["analysis"]["usedSec"]
    assert used == 2300 + row.seconds

    second = run_call(client, child_token, parent_token, parent)
    assert call_view(client, child_token, second)["recordingDisabledReason"] == (
        "ANALYSIS_QUOTA_EXCEEDED"
    )


def test_short_excluded_call_still_counts_when_stt_ran(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    call_id = run_call(
        client, child_token, parent_token, parent, parent_text="PARENT: 응 잘 지내",
        talk_seconds=30,
    )
    view = call_view(client, child_token, call_id)
    assert (view["state"], view["recordingDisabledReason"]) == ("ANALYSIS_EXCLUDED", None)
    assert len(usage_rows(client, call_id)) == 1


def test_retry_after_stt_started_records_once_and_is_not_cut(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    add_usage(client, 2300, parent["id"], child["id"])
    container = client.app.state.container
    original = container.stt.transcribe
    failures = {"left": 1}

    async def flaky(*args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise httpx.ConnectError("temporary")
        return await original(*args, **kwargs)

    container.stt.transcribe = flaky
    call_id = run_call(client, child_token, parent_token, parent, talk_seconds=600)
    assert call_view(client, child_token, call_id)["state"] == "ENDED"  # 재시도 대기
    assert len(usage_rows(client, call_id)) == 1
    # 이제 가족은 한도를 넘었지만, 이미 STT 를 시작한 통화라 끝까지 분석한다.
    assert entitlement(client, child_token)["analysis"]["usedSec"] >= 2400

    async def retry_now(session):
        await session.execute(
            update(CallRecord).where(CallRecord.id == call_id).values(
                processing_retry_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )

    run_db(client, retry_now)
    client.portal.call(container.pipeline.process, call_id)
    assert call_view(client, child_token, call_id)["state"] == "ANALYZED"
    assert len(usage_rows(client, call_id)) == 1


def test_recording_usage_twice_keeps_the_first_row(client: TestClient) -> None:
    call = CallRecord(id="call-1", parent_id="p", child_id="c", room_name="r", duration_sec=120)

    async def record_twice(session):
        await billing.record_usage(session, call, datetime(2026, 11, 30, 14, 30, tzinfo=UTC))
        call.duration_sec = 999
        await billing.record_usage(session, call, datetime(2026, 12, 5, tzinfo=UTC))

    run_db(client, record_twice)
    [row] = usage_rows(client, "call-1")
    assert (row.month, row.seconds) == ("2026-11", 120)


def test_month_boundary_follows_korean_time(client: TestClient) -> None:
    late_november = datetime(2026, 11, 30, 23, 30, tzinfo=KST)
    early_december = datetime(2026, 12, 1, 0, 30, tzinfo=KST)
    assert billing.kst_month(late_november) == "2026-11"
    assert billing.kst_month(late_november.astimezone(UTC)) == "2026-11"
    assert billing.kst_month(early_december.astimezone(UTC)) == "2026-12"
    assert billing.month_resets_at(late_november) == datetime(2026, 12, 1, tzinfo=KST)
    assert billing.month_resets_at(datetime(2026, 12, 31, 23, 0, tzinfo=KST)) == datetime(
        2027, 1, 1, tzinfo=KST
    )

    _, child, _, parent = onboard_family(client)
    settings_of(client).billing_launch_at = datetime(2026, 10, 1, tzinfo=KST)
    set_joined(client, [child["id"], parent["id"]], datetime(2026, 9, 1, tzinfo=UTC))
    add_usage(client, 2400, parent["id"], child["id"], month="2026-11")
    call = CallRecord(id="new-call", parent_id=parent["id"], child_id=child["id"])

    async def check(session):
        settings = settings_of(client)
        return (
            await billing.quota_exceeded(session, settings, call, late_november),
            await billing.quota_exceeded(session, settings, call, early_december),
        )

    assert run_db(client, check) == (True, False)


# --- RevenueCat 웹훅 -----------------------------------------------------------------------


def post_event(client: TestClient, event: dict, authorization: str | None = WEBHOOK_AUTH):
    headers = {"Authorization": authorization} if authorization is not None else {}
    return client.post(
        "/v1/webhooks/revenuecat", headers=headers, json={"api_version": "1.0", "event": event}
    )


def purchase_event(user_id: str, event_id: str, event_type: str = "INITIAL_PURCHASE",
                   expires_in: timedelta = timedelta(days=30), **extra) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "app_user_id": user_id,
        "original_app_user_id": user_id,
        "aliases": [user_id],
        "entitlement_ids": ["premium"],
        "product_id": "collog_premium:monthly",
        "store": "PLAY_STORE",
        "environment": "PRODUCTION",
        "expiration_at_ms": int((datetime.now(UTC) + expires_in).timestamp() * 1000),
        "event_timestamp_ms": int(datetime.now(UTC).timestamp() * 1000),
        **extra,
    }


def stored_entitlement(client: TestClient, user_id: str) -> UserEntitlement | None:
    async def work(session):
        return await session.get(UserEntitlement, user_id)

    return run_db(client, work)


def test_webhook_without_configuration_answers_503(client: TestClient) -> None:
    response = post_event(client, purchase_event("someone", "evt-1"))
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


def test_webhook_rejects_wrong_authorization(client: TestClient) -> None:
    settings_of(client).revenuecat_webhook_auth = WEBHOOK_AUTH
    for value in (None, "", "Bearer wrong", WEBHOOK_AUTH + "x"):
        response = post_event(client, purchase_event("someone", "evt-1"), authorization=value)
        assert response.status_code == 401, value
    assert post_event(client, {"type": "TEST"}).status_code == 422


def test_webhook_purchase_cancellation_and_expiration_without_secret_key(
    client: TestClient,
) -> None:
    child_token, child, parent_token, _ = onboard_family(client)
    end_trial(client, child["id"])
    settings_of(client).revenuecat_webhook_auth = WEBHOOK_AUTH

    bought = post_event(client, purchase_event(child["id"], "evt-buy"))
    assert bought.status_code == 200, bought.text
    assert bought.json() == {"status": "APPLIED"}
    assert entitlement(client, parent_token)["source"] == "PAID"
    row = stored_entitlement(client, child["id"])
    assert (row.store, row.product_id, row.environment, row.will_renew) == (
        "PLAY_STORE", "collog_premium:monthly", "PRODUCTION", True,
    )

    # 같은 event.id 의 재시도는 무시한다(본문이 달라도).
    again = post_event(client, purchase_event(
        child["id"], "evt-buy", "EXPIRATION", expires_in=-timedelta(minutes=1)
    ))
    assert again.json() == {"status": "DUPLICATE"}
    assert entitlement(client, child_token)["source"] == "PAID"

    # 해지(자동 갱신 끔)는 만료까지 프리미엄을 유지한다.
    cancelled = post_event(client, purchase_event(child["id"], "evt-cancel", "CANCELLATION"))
    assert cancelled.status_code == 200
    assert entitlement(client, child_token)["source"] == "PAID"
    assert stored_entitlement(client, child["id"]).will_renew is False

    expired = post_event(client, purchase_event(
        child["id"], "evt-expire", "EXPIRATION", expires_in=-timedelta(minutes=1)
    ))
    assert expired.status_code == 200
    assert entitlement(client, child_token)["plan"] == "FREE"

    async def seen(session):
        return await session.scalar(select(func.count()).select_from(RevenueCatEvent))

    assert run_db(client, seen) == 3


def test_webhook_sandbox_follows_the_setting(client: TestClient) -> None:
    child_token, child, _, _ = onboard_family(client)
    end_trial(client, child["id"])
    settings = settings_of(client)
    settings.revenuecat_webhook_auth = WEBHOOK_AUTH

    settings.revenuecat_allow_sandbox = False
    ignored = post_event(
        client, purchase_event(child["id"], "evt-sb-1", environment="SANDBOX")
    )
    assert ignored.json() == {"status": "IGNORED_SANDBOX"}
    assert stored_entitlement(client, child["id"]) is None

    settings.revenuecat_allow_sandbox = True
    applied = post_event(client, purchase_event(child["id"], "evt-sb-2", environment="SANDBOX"))
    assert applied.json() == {"status": "APPLIED"}
    assert stored_entitlement(client, child["id"]).environment == "SANDBOX"
    assert entitlement(client, child_token)["source"] == "PAID"


def premium_subscriber(days: int = 30, *, sandbox: bool = False, store: str = "play_store") -> dict:
    expires = (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "original_app_user_id": "whoever",
        "entitlements": {
            "premium": {
                "expires_date": expires,
                "grace_period_expires_date": None,
                "product_identifier": "collog_premium:monthly",
                "purchase_date": "2026-11-01T00:00:00Z",
            }
        },
        "subscriptions": {
            "collog_premium": {
                "expires_date": expires,
                "is_sandbox": sandbox,
                "store": store,
                "unsubscribe_detected_at": None,
                "billing_issues_detected_at": None,
                "period_type": "normal",
            }
        },
        "non_subscriptions": {},
    }


def test_webhook_transfer_refreshes_only_our_users_through_rest(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    settings = settings_of(client)
    settings.revenuecat_webhook_auth = WEBHOOK_AUTH
    settings.revenuecat_secret_key = "sk_test_key"
    revenuecat = client.app.state.container.revenuecat
    revenuecat.subscribers[parent["id"]] = premium_subscriber()
    grant_paid(client, child["id"], datetime.now(UTC) + timedelta(days=5))

    transferred = post_event(client, {
        "id": "evt-transfer",
        "type": "TRANSFER",
        "environment": "PRODUCTION",
        "transferred_from": [child["id"], "$RCAnonymousID:abcdef"],
        "transferred_to": [parent["id"]],
        "event_timestamp_ms": int(datetime.now(UTC).timestamp() * 1000),
    })
    assert transferred.status_code == 200, transferred.text
    assert sorted(revenuecat.requested) == sorted([child["id"], parent["id"]])
    # 옮겨 간 쪽은 구독이 생기고, 옮겨 온 쪽은 REST 응답대로 구독이 사라진다.
    assert entitlement(client, parent_token)["paidByMe"] is True
    assert stored_entitlement(client, child["id"]).active_until is None
    assert entitlement(client, child_token)["payerName"] == "김순자"


def test_will_renew_and_paid_until_follow_the_shown_subscription(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    settings_of(client).revenuecat_webhook_auth = WEBHOOK_AUTH
    assert entitlement(client, child_token)["willRenew"] is None  # 유료가 아니면 null

    post_event(client, purchase_event(child["id"], "evt-r1"))
    assert entitlement(client, parent_token)["willRenew"] is True
    post_event(client, purchase_event(child["id"], "evt-r2", "CANCELLATION"))
    assert entitlement(client, parent_token)["willRenew"] is False
    post_event(client, purchase_event(child["id"], "evt-r3", "UNCANCELLATION"))
    assert entitlement(client, child_token)["willRenew"] is True

    # REST 로 읽으면 unsubscribe_detected_at 이 있는 구독은 갱신되지 않는다.
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    cancelled = premium_subscriber(days=40)
    cancelled["subscriptions"]["collog_premium"]["unsubscribe_detected_at"] = "2026-11-20T00:00:00Z"
    client.app.state.container.revenuecat.subscribers[parent["id"]] = cancelled
    refreshed = client.post("/v1/account/entitlement/refresh", headers=auth(parent_token)).json()
    assert (refreshed["paidByMe"], refreshed["willRenew"]) == (True, False)
    # 내가 결제했으면 가족의 가장 늦은 구독(부모 40일)이 아니라 내 구독(30일·갱신)을 보인다.
    mine = entitlement(client, child_token)
    assert (mine["paidByMe"], mine["willRenew"], mine["payerName"]) == (True, True, None)
    assert datetime.fromisoformat(mine["paidUntil"]) < datetime.fromisoformat(
        refreshed["paidUntil"]
    )


def test_related_user_ids_reads_every_identity_field() -> None:
    assert related_user_ids({
        "app_user_id": "a", "original_app_user_id": "b", "aliases": ["a", "c", 3],
        "transferred_from": ["d"], "transferred_to": ["e"], "store": "x" * 50,
    }) == {"a", "b", "c", "d", "e"}


# --- 구독 새로 읽기 -------------------------------------------------------------------------


def test_refresh_without_secret_key_answers_from_stored_values(client: TestClient) -> None:
    child_token, child, _, _ = onboard_family(client)
    end_trial(client, child["id"])
    revenuecat = client.app.state.container.revenuecat
    revenuecat.subscribers[child["id"]] = premium_subscriber()
    response = client.post("/v1/account/entitlement/refresh", headers=auth(child_token))
    assert response.status_code == 200, response.text
    assert response.json()["plan"] == "FREE"
    assert revenuecat.requested == []


def test_refresh_reads_revenuecat_and_saves(client: TestClient) -> None:
    child_token, child, parent_token, _ = onboard_family(client)
    end_trial(client, child["id"])
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    revenuecat = client.app.state.container.revenuecat
    revenuecat.subscribers[child["id"]] = premium_subscriber()

    response = client.post("/v1/account/entitlement/refresh", headers=auth(child_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["plan"], body["source"], body["paidByMe"]) == ("PREMIUM", "PAID", True)
    assert revenuecat.requested == [child["id"]]
    row = stored_entitlement(client, child["id"])
    assert (row.store, row.product_id, row.environment, row.will_renew) == (
        "PLAY_STORE", "collog_premium:monthly", "PRODUCTION", True,
    )
    assert entitlement(client, parent_token)["payerName"] == "김철수"


def test_refresh_reports_upstream_failure(client: TestClient) -> None:
    child_token, child, _, _ = onboard_family(client)
    end_trial(client, child["id"])
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    container = client.app.state.container
    container.revenuecat = RevenueCatClient(
        container.settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"code": 7225})),
    )
    response = client.post("/v1/account/entitlement/refresh", headers=auth(child_token))
    assert response.status_code == 502
    assert response.json()["code"] == "UPSTREAM_ERROR"


async def test_rest_client_calls_subscribers_with_the_secret_key() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"request_date": "x", "subscriber": premium_subscriber()})

    settings = Settings(revenuecat_secret_key="sk_live_value")
    client = RevenueCatClient(settings, transport=httpx.MockTransport(respond))
    subscriber = await client.subscriber("1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed")
    [request] = seen
    assert request.method == "GET"
    assert str(request.url) == (
        "https://api.revenuecat.com/v1/subscribers/1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
    )
    assert request.headers["Authorization"] == "Bearer sk_live_value"
    assert "X-Platform" not in request.headers
    state = entitlement_from_subscriber(subscriber, "premium", allow_sandbox=True)
    assert state.active_until is not None and state.active_until > datetime.now(UTC)
    assert (state.store, state.environment, state.will_renew) == ("PLAY_STORE", "PRODUCTION", True)


def test_subscriber_parsing_rules() -> None:
    sandbox = premium_subscriber(sandbox=True)
    assert entitlement_from_subscriber(sandbox, "premium", allow_sandbox=False).active_until is None
    assert entitlement_from_subscriber(
        sandbox, "premium", allow_sandbox=True
    ).environment == "SANDBOX"
    assert entitlement_from_subscriber(
        premium_subscriber(), "other", allow_sandbox=True
    ).active_until is None

    grace = premium_subscriber(days=-1)
    grace_end = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    grace["entitlements"]["premium"]["grace_period_expires_date"] = grace_end.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert entitlement_from_subscriber(grace, "premium", allow_sandbox=True).active_until == (
        grace_end
    )

    cancelled = premium_subscriber()
    cancelled["subscriptions"]["collog_premium"]["unsubscribe_detected_at"] = "2026-11-20T00:00:00Z"
    assert entitlement_from_subscriber(cancelled, "premium", allow_sandbox=True).will_renew is False

    lifetime = {"entitlements": {"premium": {"expires_date": None, "product_identifier": "promo"}}}
    assert entitlement_from_subscriber(lifetime, "premium", allow_sandbox=True).active_until == (
        LIFETIME
    )


# --- 계정 삭제 ----------------------------------------------------------------------------


def test_account_deletion_cleans_billing_rows_but_keeps_family_usage(client: TestClient) -> None:
    child_token, child, _, parent = onboard_family(client)
    grant_paid(client, child["id"], datetime.now(UTC) + timedelta(days=10))
    shared = add_usage(client, 600, parent["id"], child["id"], call_id="shared-call")
    own = add_usage(client, 300, child["id"], "someone", call_id="own-call")

    async def seed_event(session):
        session.add(RevenueCatEvent(id="evt-del", type="INITIAL_PURCHASE", app_user_id=child["id"]))
        session.add(RevenueCatEvent(id="evt-keep", type="INITIAL_PURCHASE", app_user_id="other"))

    run_db(client, seed_event)

    deleted = client.delete("/v1/account", headers=auth(child_token))
    assert deleted.status_code == 204, deleted.text

    assert stored_entitlement(client, child["id"]) is None
    rows = {row.call_id: (row.parent_id, row.child_id, row.seconds) for row in usage_rows(client)}
    assert rows == {shared: (parent["id"], None, 600), own: (None, "someone", 300)}

    async def events(session):
        return set(await session.scalars(select(RevenueCatEvent.id)))

    assert run_db(client, events) == {"evt-keep"}


# --- 검토 반영 ----------------------------------------------------------------------------


def lapse(client: TestClient, user_id: str, *, will_renew: bool | None = True,
          checked: timedelta = timedelta(hours=1)) -> None:
    """하루 전에 만료된 구독. 마지막으로 읽은 지 checked 만큼 지났다."""
    now = datetime.now(UTC)

    async def work(session):
        session.add(UserEntitlement(
            user_id=user_id, active_until=now - timedelta(days=1), will_renew=will_renew,
            store="PLAY_STORE", updated_at=now - checked,
        ))

    run_db(client, work)


def test_lapsed_renewing_subscription_is_read_again_before_locking(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    lapse(client, child["id"])
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    revenuecat = client.app.state.container.revenuecat
    revenuecat.subscribers[child["id"]] = premium_subscriber()  # 갱신 웹훅을 놓쳤다

    # 리포트 잠금 판정 전에 그 사람만 다시 읽어 잠그지 않는다.
    assert report(client, parent_token, parent["id"]).status_code == 200
    assert revenuecat.requested == [child["id"]]
    assert entitlement(client, parent_token)["source"] == "PAID"
    assert revenuecat.requested == [child["id"]]  # 이제 유효하니 다시 부르지 않는다


def test_lapsed_check_runs_once_per_ten_minutes_and_skips_cancelled(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    lapse(client, child["id"])
    lapse(client, parent["id"], will_renew=False)
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    revenuecat = client.app.state.container.revenuecat

    for _ in range(2):
        assert entitlement(client, child_token)["plan"] == "FREE"
    # 해지해 둔 부모 구독은 읽지 않고, 아이 구독은 10분 안에 한 번만 읽는다.
    assert revenuecat.requested == [child["id"]]


def test_lapsed_check_failure_falls_back_to_stored_values(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    lapse(client, child["id"])
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    container = client.app.state.container
    container.revenuecat = RevenueCatClient(
        container.settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})),
    )
    assert entitlement(client, child_token)["plan"] == "FREE"
    assert report(client, parent_token, parent["id"]).status_code == 402
    # 실패해도 10분 뒤에 다시 보도록 시각은 남긴다.
    row = stored_entitlement(client, child["id"])
    assert datetime.now(UTC) - billing.aware(row.updated_at) < timedelta(minutes=1)


def test_lapsed_check_is_off_without_secret_key_or_billing(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    lapse(client, child["id"])
    revenuecat = client.app.state.container.revenuecat
    revenuecat.subscribers[child["id"]] = premium_subscriber()
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    entitlement(client, child_token)  # 결제 꺼짐
    end_trial(client, child["id"], parent["id"])
    settings_of(client).revenuecat_secret_key = ""
    assert entitlement(client, child_token)["plan"] == "FREE"  # 비밀 키 없음
    assert revenuecat.requested == []


def test_quota_check_reads_a_lapsed_subscription_before_excluding(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    add_usage(client, 2400, parent["id"], child["id"])
    lapse(client, child["id"])
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    revenuecat = client.app.state.container.revenuecat
    revenuecat.subscribers[child["id"]] = premium_subscriber()

    call_id = run_call(client, child_token, parent_token, parent)
    assert call_view(client, child_token, call_id)["state"] == "ANALYZED"
    assert revenuecat.requested == [child["id"]]


def test_saving_an_entitlement_is_an_upsert(client: TestClient) -> None:
    """refresh 와 첫 구매 웹훅이 겹쳐도 PK 위반이 없게 INSERT … ON CONFLICT DO UPDATE 로 쓴다."""
    _, child, _, _ = onboard_family(client)
    grant_paid(client, child["id"], datetime.now(UTC) + timedelta(days=1))
    engine = client.app.state.container.database.engine.sync_engine
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        until = datetime.now(UTC).replace(microsecond=0) + timedelta(days=9)

        async def save(session):
            state = EntitlementState(active_until=until, store="APP_STORE", will_renew=False)
            await billing.save_entitlement(session, child["id"], state, datetime.now(UTC))

        run_db(client, save)
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert any("ON CONFLICT (user_id) DO UPDATE" in item for item in statements)
    row = stored_entitlement(client, child["id"])
    assert (billing.aware(row.active_until), row.store, row.will_renew) == (
        until, "APP_STORE", False,
    )


def test_refresh_skips_revenuecat_when_billing_is_off_or_called_again_soon(
    client: TestClient,
) -> None:
    child_token, child, _, _ = onboard_family(client)
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    revenuecat = client.app.state.container.revenuecat
    revenuecat.subscribers[child["id"]] = premium_subscriber()

    off = client.post("/v1/account/entitlement/refresh", headers=auth(child_token))
    assert (off.status_code, revenuecat.requested) == (200, [])

    end_trial(client, child["id"])
    for _ in range(2):
        response = client.post("/v1/account/entitlement/refresh", headers=auth(child_token))
        assert response.json()["source"] == "PAID"
    assert revenuecat.requested == [child["id"]]


def test_webhook_authorization_ignores_surrounding_whitespace(client: TestClient) -> None:
    child_token, child, _, _ = onboard_family(client)
    settings_of(client).revenuecat_webhook_auth = f"  {WEBHOOK_AUTH}\n"
    response = post_event(client, purchase_event(child["id"], "evt-ws"))
    assert response.status_code == 200, response.text


def test_event_mode_only_moves_expiry_forward_and_honours_grace(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])
    settings_of(client).revenuecat_webhook_auth = WEBHOOK_AUTH

    post_event(client, purchase_event(child["id"], "evt-f1", expires_in=timedelta(days=30)))
    first = datetime.fromisoformat(entitlement(client, child_token)["paidUntil"])
    # 늦게 도착한 옛 갱신 이벤트(만료 10일)는 만료를 당기지 않는다.
    post_event(client, purchase_event(
        child["id"], "evt-f2", "RENEWAL", expires_in=timedelta(days=10)
    ))
    assert datetime.fromisoformat(entitlement(client, child_token)["paidUntil"]) == first
    # 만료(EXPIRATION)만 접근을 끊는다.
    post_event(client, purchase_event(
        child["id"], "evt-f3", "EXPIRATION", expires_in=-timedelta(minutes=1)
    ))
    assert entitlement(client, child_token)["plan"] == "FREE"

    # 결제 문제(BILLING_ISSUE)는 유예 끝까지 유지한다.
    grace = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    post_event(client, purchase_event(
        parent["id"], "evt-f4", "BILLING_ISSUE", expires_in=-timedelta(hours=1),
        grace_period_expiration_at_ms=int(grace.timestamp() * 1000),
    ))
    body = entitlement(client, parent_token)
    assert (body["source"], datetime.fromisoformat(body["paidUntil"])) == ("PAID", grace)


def test_usage_before_launch_is_not_counted(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    end_trial(client, child["id"], parent["id"])  # 20일 전에 켜짐
    before = datetime.now(UTC) - timedelta(days=25)
    add_usage(client, 2400, parent["id"], child["id"], call_id="before-launch", created_at=before)
    add_usage(client, 100, parent["id"], child["id"], call_id="after-launch")

    assert entitlement(client, child_token)["analysis"]["usedSec"] == 100
    call_id = run_call(client, child_token, parent_token, parent)
    assert call_view(client, child_token, call_id)["state"] == "ANALYZED"

    settings_of(client).billing_launch_at = None  # 꺼지면 실제 사용량 전부
    assert entitlement(client, child_token)["analysis"]["usedSec"] >= 2500


def test_failed_stt_releases_the_usage_but_later_failures_keep_it(client: TestClient) -> None:
    child_token, child, parent_token, parent = onboard_family(client)
    container = client.app.state.container
    original_stt = container.stt.transcribe

    async def broken_stt(*args, **kwargs):
        raise ValueError("not retried")

    container.stt.transcribe = broken_stt
    failed = run_call(client, child_token, parent_token, parent, talk_seconds=60)
    assert call_view(client, child_token, failed)["state"] == "ANALYSIS_FAILED"
    assert usage_rows(client, failed) == []

    # STT 가 끝나 전사가 남은 뒤에 실패하면 분량은 쓴 것으로 둔다.
    container.stt.transcribe = original_stt

    async def broken_acoustics(*args, **kwargs):
        raise ValueError("not retried")

    container.acoustics.analyze = broken_acoustics
    late = run_call(client, child_token, parent_token, parent, talk_seconds=60)
    assert call_view(client, child_token, late)["state"] == "ANALYSIS_FAILED"
    assert len(usage_rows(client, late)) == 1


def test_account_deletion_asks_revenuecat_to_delete_the_customer(client: TestClient) -> None:
    child_token, child, _, _ = onboard_family(client)
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    end_trial(client, child["id"])
    revenuecat = client.app.state.container.revenuecat
    assert client.delete("/v1/account", headers=auth(child_token)).status_code == 204
    assert revenuecat.deleted == [child["id"]]


def test_account_deletion_skips_revenuecat_when_billing_is_off(client: TestClient) -> None:
    child_token, _, _, _ = onboard_family(client)
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    revenuecat = client.app.state.container.revenuecat
    assert client.delete("/v1/account", headers=auth(child_token)).status_code == 204
    assert revenuecat.deleted == []


def test_account_deletion_finishes_when_revenuecat_fails(client: TestClient) -> None:
    child_token, child, _, _ = onboard_family(client)
    settings_of(client).revenuecat_secret_key = "sk_test_key"
    end_trial(client, child["id"])
    container = client.app.state.container
    container.revenuecat = RevenueCatClient(
        container.settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})),
    )
    assert client.delete("/v1/account", headers=auth(child_token)).status_code == 204

    async def remaining(session):
        return await session.get(User, child["id"])

    assert run_db(client, remaining) is None


async def test_rest_client_deletes_the_customer_and_treats_404_as_done() -> None:
    seen: list[httpx.Request] = []
    status = {"code": 200}

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status["code"], json={"app_user_id": "u", "deleted": True})

    client = RevenueCatClient(
        Settings(revenuecat_secret_key="sk_live_value"), transport=httpx.MockTransport(respond)
    )
    await client.delete_subscriber("u-1")
    status["code"] = 404
    await client.delete_subscriber("u-1")
    status["code"] = 401
    with pytest.raises(RevenueCatError):
        await client.delete_subscriber("u-1")
    assert {(request.method, str(request.url)) for request in seen} == {
        ("DELETE", "https://api.revenuecat.com/v1/subscribers/u-1")
    }
    assert all(request.headers["Authorization"] == "Bearer sk_live_value" for request in seen)
