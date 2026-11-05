from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select

from app.config import Settings
from app.database import Database
from app.models import (
    AcousticFeature,
    AnalysisMode,
    AssetKind,
    AssetStatus,
    AudioAsset,
    CallRecord,
    CallState,
    HealthExtraction,
    Metric,
    Transcript,
)

TERMINAL_STATES = {
    CallState.ANALYZED.value,
    CallState.ANALYSIS_EXCLUDED.value,
    CallState.ANALYSIS_FAILED.value,
}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: Any
    expected: str


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def snapshot(database: Database, call_id: str | None) -> dict[str, Any] | None:
    async with database.sessions() as session:
        if call_id:
            call = await session.get(CallRecord, call_id)
        else:
            call = await session.scalar(select(CallRecord).order_by(desc(CallRecord.created_at)))
        if call is None:
            return None
        assets = list(
            await session.scalars(select(AudioAsset).where(AudioAsset.call_id == call.id))
        )
        transcript = await session.scalar(
            select(Transcript).where(Transcript.call_id == call.id)
        )
        extraction_query = select(HealthExtraction).where(HealthExtraction.call_id == call.id)
        feature_query = select(AcousticFeature).where(AcousticFeature.call_id == call.id)
        if call.analysis_mode == AnalysisMode.BOTH.value:
            # 두 사람을 분석한 통화는 결과가 사람마다 있다. 이 검증은 부모(parent_id) 몫을 본다.
            extraction_query = extraction_query.where(
                HealthExtraction.subject_user_id == call.parent_id
            )
            feature_query = feature_query.where(AcousticFeature.subject_user_id == call.parent_id)
        extraction = await session.scalar(extraction_query)
        features = list(await session.scalars(feature_query))
        return {
            "call": {
                "id": call.id,
                "parentId": call.parent_id,
                "state": call.state,
                "roomName": call.room_name,
                "acceptedAt": call.accepted_at,
                "endedAt": call.ended_at,
                "durationSec": call.duration_sec,
                "parentSpeechSec": call.parent_speech_sec,
                "rawAudioPurgedAt": call.raw_audio_purged_at,
                "processingError": call.processing_error,
            },
            "assets": [
                {
                    "kind": asset.kind,
                    "ownerUserId": asset.owner_user_id,
                    "status": asset.status,
                    "egressId": asset.egress_id,
                    "durationSec": asset.duration_sec,
                    "purgedAt": asset.purged_at,
                }
                for asset in assets
            ],
            "transcript": (
                {
                    "provider": transcript.provider,
                    "excluded": transcript.excluded,
                    "exclusionReason": transcript.exclusion_reason,
                    "parentSpeechSec": transcript.parent_speech_sec,
                    "segments": transcript.segments,
                }
                if transcript
                else None
            ),
            "extraction": (
                {
                    "parseStatus": extraction.parse_status,
                    "symptom": extraction.symptom,
                    "medication": extraction.medication,
                    "activity": extraction.activity,
                    "sleep": extraction.sleep,
                }
                if extraction
                else None
            ),
            "features": [
                {
                    "metric": feature.metric,
                    "value": feature.value,
                    "unit": feature.unit,
                    "status": feature.status,
                    "reason": feature.unmeasurable_reason,
                    "source": feature.audio_source,
                }
                for feature in features
            ],
        }


def evaluate(data: dict[str, Any], minimum_duration: int, allow_excluded: bool) -> dict[str, Any]:
    call = data["call"]
    assets = data["assets"]
    transcript = data["transcript"]
    features = data["features"]
    by_kind = {asset["kind"]: asset for asset in assets}
    parent_asset = by_kind.get(AssetKind.WEBRTC_EGRESS_PARENT.value)
    child_asset = by_kind.get(AssetKind.WEBRTC_EGRESS_CHILD.value)
    # 두 사람을 분석하면 원시 오디오도 둘이다. 부모가 올린 것을 본다.
    raw_asset = next(
        (
            asset
            for asset in assets
            if asset["kind"] == AssetKind.DEVICE_RAW.value
            and asset["ownerUserId"] == call["parentId"]
        ),
        None,
    )
    speakers = {
        segment.get("speaker")
        for segment in (transcript or {}).get("segments", [])
        if segment.get("text", "").strip()
    }
    metrics = {feature["metric"] for feature in features}
    expected_metrics = {metric.value for metric in Metric}
    accepted_state = (
        {CallState.ANALYZED.value, CallState.ANALYSIS_EXCLUDED.value}
        if allow_excluded
        else {CallState.ANALYZED.value}
    )

    checks = [
        Check(
            "parent accepted CallKit call",
            call["acceptedAt"] is not None,
            call["acceptedAt"],
            "timestamp",
        ),
        Check("call ended", call["endedAt"] is not None, call["endedAt"], "timestamp"),
        Check(
            "minimum conversation duration",
            (call["durationSec"] or 0) >= minimum_duration,
            call["durationSec"],
            f">= {minimum_duration}s",
        ),
        Check("parent LiveKit track egress", parent_asset is not None, parent_asset, "present"),
        Check("child LiveKit track egress", child_asset is not None, child_asset, "present"),
        Check(
            "both egress workers started",
            bool(
                parent_asset
                and parent_asset["egressId"]
                and child_asset
                and child_asset["egressId"]
            ),
            {
                "parent": parent_asset and parent_asset["egressId"],
                "child": child_asset and child_asset["egressId"],
            },
            "two egress IDs",
        ),
        Check("parent device raw PCM", raw_asset is not None, raw_asset, "present"),
        Check(
            "all raw audio purged",
            bool(assets) and all(asset["status"] == AssetStatus.PURGED.value for asset in assets),
            {asset["kind"]: asset["status"] for asset in assets},
            "all PURGED",
        ),
        Check(
            "purge audit timestamp",
            call["rawAudioPurgedAt"] is not None,
            call["rawAudioPurgedAt"],
            "timestamp",
        ),
        Check(
            "STT result stored",
            transcript is not None,
            transcript and transcript["provider"],
            "provider",
        ),
        Check(
            "both speakers transcribed",
            {"PARENT", "CHILD"}.issubset(speakers),
            sorted(speakers),
            "PARENT + CHILD",
        ),
        Check(
            "four acoustic metrics stored",
            expected_metrics.issubset(metrics),
            sorted(metrics),
            "four P0-16 metrics",
        ),
        Check(
            "analysis terminal state",
            call["state"] in accepted_state,
            call["state"],
            " or ".join(sorted(accepted_state)),
        ),
        Check("no pipeline error", not call["processingError"], call["processingError"], "null"),
    ]
    warnings: list[str] = []
    extraction = data["extraction"]
    if extraction is None:
        warnings.append("LLM extraction record가 없습니다.")
    elif extraction["parseStatus"] != "OK":
        warnings.append(
            "Gemini extraction이 FAILED입니다. 통화 미디어와 AI-2 검증 결과는 별개입니다."
        )
    if transcript and transcript["excluded"]:
        warnings.append(
            f"부모 발화 부족으로 분석 제외됨: {transcript['exclusionReason']} "
            f"({transcript['parentSpeechSec']}s)"
        )

    serialized = [
        {key: json_value(value) for key, value in asdict(item).items()} for item in checks
    ]
    return {
        "passed": all(item.passed for item in checks),
        "callId": call["id"],
        "state": call["state"],
        "checks": serialized,
        "warnings": warnings,
        "evidence": {
            "roomName": call["roomName"],
            "parentSpeechSec": call["parentSpeechSec"],
            "features": features,
            "extraction": extraction,
        },
    }


async def verify(args: argparse.Namespace) -> int:
    database = Database(Settings().database_url)
    deadline = time.monotonic() + args.timeout
    data: dict[str, Any] | None = None
    try:
        while time.monotonic() <= deadline:
            data = await snapshot(database, args.call_id)
            if data:
                state = data["call"]["state"]
                purge_finished = bool(data["assets"]) and all(
                    asset["status"] == AssetStatus.PURGED.value for asset in data["assets"]
                )
                # ANALYZED/EXCLUDED commit이 원본 삭제보다 먼저 일어나므로 삭제 audit까지
                # 기다린다. FAILED는 원인을 바로 보여준다.
                if state == CallState.ANALYSIS_FAILED.value or (
                    state in TERMINAL_STATES and purge_finished
                ):
                    break
            await asyncio.sleep(args.poll_interval)
        if data is None:
            print(
                json.dumps(
                    {"passed": False, "error": "통화를 찾을 수 없습니다"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        result = evaluate(data, args.minimum_duration, args.allow_excluded)
        if data["call"]["state"] not in TERMINAL_STATES:
            result["passed"] = False
            result["warnings"].append(f"{args.timeout}초 안에 분석이 끝나지 않았습니다.")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=json_value))
        return 0 if result["passed"] else 1
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="iPhone 2대 통화의 LiveKit 양 트랙→STT→AI-2→원본 폐기를 자동 검증한다"
    )
    parser.add_argument("--call-id", help="VoipCallCenter 로그의 callId. 생략하면 최신 통화")
    parser.add_argument("--timeout", type=int, default=180, help="분석 완료 대기 초")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--minimum-duration", type=int, default=30)
    parser.add_argument(
        "--allow-excluded",
        action="store_true",
        help="부모 발화 부족 ANALYSIS_EXCLUDED도 통과로 허용",
    )
    raise SystemExit(asyncio.run(verify(parser.parse_args())))


if __name__ == "__main__":
    main()
