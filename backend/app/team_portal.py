from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from app.config import Settings

FEATURES = [
    ("AI-1 · STT", "완료", "Deepgram Nova-3 한국어 실호출과 Track Egress E2E 검증"),
    ("AI-1 · LLM", "완료", "부모-only 근거 segment·polarity·semantic validator"),
    ("되묻기 감지", "완료", "한국어 규칙·제외 규칙·3초 병합·통화/리포트 집계"),
    ("AI-2 · 음향", "Prototype", "word timing·pYIN·기침 후보 transient 실제 계산"),
    ("백엔드", "Prototype", "인증·동의·통화·분석·리포트 API와 원본 폐기"),
    ("iOS/APNs", "Prototype", "Swift 앱·APNs sandbox 구현, iPhone 2대 E2E 필요"),
    ("연결 질문 TTS", "Prototype", "ElevenLabs MP3 캐시·서명 URL, iOS 로컬 폴백"),
]


def build_team_status(settings: Settings) -> dict[str, Any]:
    livekit_ready = bool(settings.livekit_api_key and settings.livekit_api_secret)
    apns_ready = bool(
        settings.apns_voip_enabled
        and settings.apns_team_id
        and settings.apns_key_id
        and settings.apns_bundle_id
        and settings.apns_private_key_path
    )
    elevenlabs_ready = bool(
        settings.elevenlabs_api_key.startswith("sk_") and settings.elevenlabs_voice_id
    )
    elevenlabs_selected = settings.question_tts_provider in {"elevenlabs", "elevenlabs_direct"}
    direct_tts = settings.question_tts_provider == "elevenlabs_direct"
    return {
        "service": "Collog backend",
        "status": "ok",
        "environment": settings.app_env,
        "checkedAt": datetime.now(UTC).isoformat(),
        "externalMode": "mock" if settings.mock_external_services else "real",
        "providers": {
            "deepgram": {
                "configured": bool(settings.deepgram_api_key),
                "model": settings.deepgram_model,
                "language": settings.deepgram_language,
            },
            "gemini": {
                "configured": bool(settings.gemini_api_key),
                "model": settings.gemini_model,
                "maxOutputTokens": settings.gemini_max_output_tokens,
            },
            "livekit": {"configured": livekit_ready, "mode": "self-hosted"},
            "apnsVoip": {"configured": apns_ready, "enabled": settings.apns_voip_enabled},
            "questionTts": {
                "configured": not elevenlabs_selected or elevenlabs_ready,
                "provider": settings.question_tts_provider if elevenlabs_selected else "ios-local",
                "mode": (
                    "direct-websocket" if direct_tts and elevenlabs_ready
                    else "remote-asset" if elevenlabs_selected and elevenlabs_ready else "ios-local"
                ),
                "language": "ko-KR",
                "model": settings.elevenlabs_model if elevenlabs_selected else None,
                "fallback": "ios-local",
                "deepgramKoreanSupported": False,
            },
            "storage": {"backend": settings.storage_backend},
        },
        "features": [
            {"name": name, "status": status, "detail": detail} for name, status, detail in FEATURES
        ],
        "links": {
            "swagger": "/docs",
            "openapi": "/openapi.json",
            "handoff": f"{settings.team_portal_repository_url}/blob/main/HANDOFF.md",
            "transcriptDesign": (
                f"{settings.team_portal_repository_url}"
                "/blob/main/backend/docs/ai-transcript-design.md"
            ),
            "acousticDesign": (
                f"{settings.team_portal_repository_url}/blob/main/backend/docs/acoustic-design.md"
            ),
        },
    }


def render_team_portal(settings: Settings) -> str:
    status = build_team_status(settings)
    providers = status["providers"]

    def readiness(value: bool) -> str:
        return "설정됨" if value else "미설정"

    feature_cards = "".join(
        f"""
        <article class="feature-card">
          <div class="feature-top"><h3>{escape(item["name"])}</h3>
            <span class="badge">{escape(item["status"])}</span></div>
          <p>{escape(item["detail"])}</p>
        </article>
        """
        for item in status["features"]
    )
    provider_rows = "".join(
        (
            f"<tr><th scope='row'>{escape(name)}</th>"
            f"<td>{escape(detail)}</td><td class='{css_class}'>{escape(value)}</td></tr>"
        )
        for name, detail, value, css_class in (
            (
                "Deepgram",
                f"{providers['deepgram']['model']} · {providers['deepgram']['language']}",
                readiness(providers["deepgram"]["configured"]),
                "ready" if providers["deepgram"]["configured"] else "waiting",
            ),
            (
                "Gemini",
                f"{providers['gemini']['model']} · {providers['gemini']['maxOutputTokens']} tokens",
                readiness(providers["gemini"]["configured"]),
                "ready" if providers["gemini"]["configured"] else "waiting",
            ),
            (
                "LiveKit",
                "self-hosted Server + Track Egress",
                readiness(providers["livekit"]["configured"]),
                "ready" if providers["livekit"]["configured"] else "waiting",
            ),
            (
                "APNs VoIP",
                "PushKit + CallKit 실기기",
                readiness(providers["apnsVoip"]["configured"]),
                "ready" if providers["apnsVoip"]["configured"] else "waiting",
            ),
            (
                "질문 TTS",
                (
                    f"ElevenLabs {providers['questionTts']['model']} · iOS 폴백"
                    if providers["questionTts"]["provider"] in {"elevenlabs", "elevenlabs_direct"}
                    else "iOS AVSpeechSynthesizer · ko-KR"
                ),
                readiness(providers["questionTts"]["configured"]),
                "ready" if providers["questionTts"]["configured"] else "waiting",
            ),
        )
    )
    links = status["links"]
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#152139">
  <meta http-equiv="refresh" content="30">
  <title>Collog Team Hub</title>
  <style>
    :root {{ color-scheme: light; --ink:#152139; --muted:#617087; --paper:#f4f6f8;
      --card:#fff; --line:#dce2e8; --accent:#2f6feb; --mint:#dff6ec; --amber:#fff1cc; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--paper); color:var(--ink); font-family:-apple-system,
      BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif; line-height:1.55; }}
    main {{ width:min(1080px,calc(100% - 32px)); margin:0 auto; padding:32px 0 56px; }}
    .hero {{ background:var(--ink); color:white; padding:28px; border-radius:24px;
      box-shadow:0 16px 42px rgba(21,33,57,.16); }}
    .eyebrow {{ margin:0 0 8px; color:#a9c7ff; font-size:.78rem; font-weight:800;
      letter-spacing:.12em; text-transform:uppercase; }}
    h1 {{ margin:0; font-size:clamp(2rem,7vw,4.2rem); line-height:1; letter-spacing:-.05em; }}
    .hero p {{ max-width:720px; margin:16px 0 0; color:#d7dfeb; }}
    .live {{ display:inline-flex; align-items:center; gap:8px; margin-top:20px; padding:8px 12px;
      border:1px solid #3c4b65; border-radius:999px; font-size:.82rem; }}
    .dot {{ width:9px; height:9px; border-radius:50%; background:#4ade80;
      box-shadow:0 0 0 5px rgba(74,222,128,.12); }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin:22px 0 0; }}
    .actions a {{ color:white; text-decoration:none; border:1px solid #66728a; border-radius:12px;
      padding:10px 14px; font-weight:700; }}
    .actions a.primary {{ background:white; color:var(--ink); border-color:white; }}
    section {{ margin-top:34px; }}
    h2 {{ margin:0 0 14px; font-size:1.35rem; letter-spacing:-.025em; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
    .feature-card, .panel {{ background:var(--card); border:1px solid var(--line);
      border-radius:18px; padding:18px; }}
    .feature-top {{ display:flex; justify-content:space-between; gap:10px;
      align-items:flex-start; }}
    h3 {{ margin:0; font-size:1rem; }}
    .feature-card p {{ margin:12px 0 0; color:var(--muted); font-size:.9rem; }}
    .badge {{ background:#e8eef9; color:#315181; border-radius:999px; padding:4px 8px;
      white-space:nowrap; font-size:.72rem; font-weight:800; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:13px 8px; border-bottom:1px solid var(--line); text-align:left;
      font-size:.9rem; }}
    tr:last-child th,tr:last-child td {{ border-bottom:0; }}
    td:nth-child(2) {{ color:var(--muted); }} td:last-child {{ text-align:right; font-weight:800; }}
    .ready {{ color:#14754c; }} .waiting {{ color:#9a6700; }}
    .flow {{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; align-items:center; }}
    .flow span {{ background:#eef2f7; border-radius:12px; padding:12px 8px; text-align:center;
      font-weight:750; font-size:.82rem; }}
    .flow b {{ text-align:center; color:var(--muted); }}
    .footnote {{ color:var(--muted); font-size:.82rem; margin-top:14px; }}
    @media (max-width:760px) {{ main {{ width:min(100% - 20px,680px); padding-top:10px; }}
      .hero {{ border-radius:18px; padding:22px; }} .grid {{ grid-template-columns:1fr; }}
      .flow {{ grid-template-columns:1fr; }} .flow b {{ transform:rotate(90deg); }}
      th,td {{ display:block; border:0; padding:5px 0; }} tr {{ display:block;
        padding:10px 0; border-bottom:1px solid var(--line); }}
      td:last-child {{ text-align:left; }} }}
  </style>
</head>
<body><main>
  <header class="hero">
    <p class="eyebrow">Collog · backend & AI</p>
    <h1>Team Hub</h1>
    <p>콜록의 현재 연결 상태, 구현 범위와 다음 설계를 한 화면에서 공유합니다.
      이 페이지는 실제 백엔드가 렌더링하며 30초마다 새로고침됩니다.</p>
    <div class="live"><span class="dot"></span>
      Backend connected · {escape(status["environment"])}</div>
    <nav class="actions" aria-label="개발 문서">
      <a class="primary" href="{escape(links["swagger"])}">Swagger API</a>
      <a href="{escape(links["handoff"])}">HANDOFF</a>
      <a href="{escape(links["transcriptDesign"])}">AI-1 설계</a>
      <a href="{escape(links["acousticDesign"])}">AI-2 설계</a>
    </nav>
  </header>
  <section><h2>구현 트랙</h2><div class="grid">{feature_cards}</div></section>
  <section><h2>연결 상태</h2><div class="panel"><table><tbody>{provider_rows}</tbody></table>
    <p class="footnote">보안을 위해 키·토큰·내부 주소는 표시하지 않고 설정 여부만 제공합니다.</p>
  </div></section>
  <section><h2>통화 후 분석 흐름</h2><div class="panel"><div class="flow">
    <span>Track Egress</span><b>→</b><span>Deepgram STT</span><b>→</b>
    <span>Gemini + 음향 분석</span>
  </div><p class="footnote">LLM은 통화 연결 경로가 아니라 종료 후 비동기 분석 경로에
    있습니다.</p></div></section>
</main></body></html>"""
