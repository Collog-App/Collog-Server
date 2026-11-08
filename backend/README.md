# Collog backend

콜록의 P0 API, self-hosted LiveKit 통화 녹음, Deepgram Nova-3 한국어 STT,
Gemini 건강 대화 구조화 파이프라인이다.

## 구현 범위

- 휴대폰 OTP 인증, 부모 초대, append-only 민감정보 동의, 질환 프로필
- 질환별 오늘의 질문과 통화 시작/수락/거절/종료 API
- self-hosted LiveKit Server와 Track Egress 기반 화자별 Opus/OGG 녹음
- iOS PushKit VoIP 푸시와 CallKit 수신 통화용 APNs provider
- 부모 기기 분석용 PCM 파일의 presigned upload
- Deepgram `nova-3`, `language=ko` 사전 녹음 STT
- Gemini parent-only evidence JSON 기반 증상·복약·활동·수면 추출과 polarity/근거 저장
- 부모 utterance의 되묻는 표현 규칙 탐지, 3초 병합, 통화·리포트 집계
- Deepgram word timing 발화 속도/휴지와 PCM pYIN F0/기침 후보 transient 분석
- 부모 발화 20초 미만 제외, 오디오 분석 직후 폐기, 실패 파일 24시간 내 폐기
- 앵커/롤링 기준선, MAD robust z, 변화 신호, immutable 리포트 스냅샷 API
- 모바일 브라우저와 Swift `WKWebView`에서 여는 팀 상태 포털

음향 지표 4종은 실제 값을 계산한다. 다만 기침은 의료용 분류가 아니라 versioned
`transient-heuristic-v1` 후보 detector이며 실제 cough/hard-negative validation 전까지 UI에서
확정 기침이나 질환 신호로 표현하면 안 된다. PCM 품질이나 word timing이 부족하면 관련 값은
명시적인 `UNMEASURABLE` 사유와 함께 저장된다.

## 빠른 로컬 실행 — API만

외부 서비스 없이 전체 API 흐름을 확인할 수 있다. 개발 OTP는 `000000`이다.

```bash
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --port 8080
```

- Swagger UI: <http://localhost:8080/docs>
- 상태 확인: <http://localhost:8080/v1/health>
- 팀 포털: <http://localhost:8080/team>
- 팀 상태 JSON: <http://localhost:8080/team/status.json>
- SQLite와 업로드 파일: `.data/`

`MOCK_EXTERNAL_SERVICES=true`에서는 LiveKit, Deepgram, Gemini 호출이 모의 구현으로
대체된다. 운영 또는 통합 테스트에서만 `false`로 둔다.

### 스키마가 바뀌었을 때

기동 시 모델과 DB 스키마를 비교한다. `SQLAlchemy`의 `create_all()`은 기존 테이블을 ALTER하지
않으므로, 이 검사가 없으면 스키마 변경이 조용히 무시되고 런타임에야 터진다.

| 상황 | `SCHEMA_AUTO_RESET=true` (기본, 로컬) | `SCHEMA_AUTO_RESET=false` (배포) |
|---|---|---|
| 모델과 DB가 같음 | 아무것도 안 함 | 아무것도 안 함 |
| 신규 테이블만 늘어남 | 그 테이블만 생성. **데이터 보존** | 기동 거부 |
| 기존 테이블의 컬럼이 어긋남 | **DB 재생성** + WARNING | 기동 거부 |

`git pull` 후 "DB를 재생성했습니다" WARNING이 보이면 데모 데이터를 다시 채운다.

```bash
uv run python scripts/seed_demo_family.py
```

배포 환경은 `SCHEMA_AUTO_RESET=false`로 띄운다. 그 환경의 스키마는 Alembic이 소유하며 guard는
검증만 한다. 자세한 근거와 migration 절차는 `docs/schema-management-design.md`에 있다.

### 기침 detector 모델 (선택)

`COUGH_EVENTS`는 기본값이 `UNMEASURABLE`이라 모델 없이도 나머지 지표는 전부 동작한다.
기침 detector를 직접 돌려 보려면 한 번 받아 둔다.

```bash
# https://huggingface.co/google/hear 에서 약관 동의 후 read 토큰 발급
HF_TOKEN=hf_... uv run --with tensorflow --with tf2onnx python scripts/fetch_cough_model.py
```

가중치는 Health AI Developer Foundations 약관 대상이라 저장소에 커밋하지 않는다.
`backend/models/`는 `.gitignore` 대상이며 산출물은 약 5 MB다. 변환에만 TensorFlow가 필요하고
런타임은 `onnxruntime`만 쓴다. 파일이 없으면 `MODEL_UNAVAILABLE`, sha256이 다르면
`MODEL_CHECKSUM_MISMATCH`로 떨어지며 어느 경우에도 숫자를 만들지 않는다.

## self-hosted 전체 스택

Docker Compose는 다음 서비스를 한 번에 실행한다.

```text
앱 ── WebRTC ── LiveKit Server ── Redis
                       │
                  Egress worker ── MinIO
                                      │
백엔드 ── PostgreSQL       Deepgram/Gemini API
   └──── APNs VoIP push ── iOS PushKit/CallKit
```

`.env`에 두 API 키와 기기에서 접근 가능한 주소를 넣는다.

```dotenv
DEEPGRAM_API_KEY=...
GEMINI_API_KEY=...

# 실제 휴대폰이라면 개발 PC의 LAN 주소를 사용한다.
PUBLIC_BASE_URL=http://192.168.0.10:8080
LIVEKIT_URL=ws://192.168.0.10:7880
S3_PUBLIC_ENDPOINT_URL=http://192.168.0.10:9000
```

그다음 실행한다.

```bash
docker compose up --build
docker compose exec backend python -m scripts.preflight_two_iphone
```

| 서비스 | 주소 |
|---|---|
| Backend | `http://localhost:8080` |
| LiveKit signaling | `ws://localhost:7880` |
| LiveKit TCP | `localhost:7881` |
| LiveKit UDP | `localhost:7882/udp` |
| MinIO API | `http://localhost:9000` |
| MinIO console | `http://localhost:9001` |

### Docker 없이 통화만 시험하기

컨테이너 런타임이 없는 개발 PC에서는 LiveKit과 MinIO를 네이티브로 실행한다.

```bash
brew install livekit minio minio-mc

livekit-server --config deploy/livekit-local.yaml
MINIO_ROOT_USER=collog-minio MINIO_ROOT_PASSWORD=collog-minio-secret \
  minio server ~/.collog/minio --address :9000
mc alias set collog http://127.0.0.1:9000 collog-minio collog-minio-secret
mc mb --ignore-existing collog/collog-audio

uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

이 구성에는 Egress worker가 없다. 기본값에서는 실시간 통화만 동작하고 Track Egress 기반의
부모/자녀 분리 전사도 생성되지 않는다. 부모 기기가 올린 분석용 PCM만으로 개발 파이프라인을
끝까지 시험하려면 `.env`에 `ALLOW_RAW_ONLY_ANALYSIS=true`를 넣는다. 이때 transcript는 부모
단일 화자로만 생성된다. 양쪽 분리 녹음까지 검증할 때에는 compose 스택을 쓴다.

앱에는 아직 초대·동의·질환 프로필 화면이 없으므로, 실기기 통화를 시험하기 전에 자녀에게
연결된 부모 계정을 만들어 둔다.

```bash
uv run python -m scripts.seed_demo_family --child-phone 01000000002
```

저장된 WAV/OGG로 provider와 분석기만 재실행하려면 `scripts.replay_call`을 쓴다. `--raw-audio`
만 전달한 경우에는 스크립트가 이번 실행에서 raw-only mode를 자동으로 켠다.

Egress는 LiveKit과 같은 Redis를 사용한다. 통화 수락 시 이미 publish된 자녀의 microphone
track을 조회하고, 수락 뒤 publish되는 부모 track은 서명된 `track_published` 웹훅으로 받아
각각 Opus/OGG Track Egress를 시작한다. `Participant Egress + OGG`는 비디오 transcode 경로와
호환되지 않으므로 사용하지 않는다. Egress 완료 웹훅은 LiveKit JWT 서명을 검증한 뒤에만
파일을 분석 큐에 넣는다.

개발용 LiveKit/MinIO/PostgreSQL 비밀값은 compose 파일에 고정되어 있다. 외부에 노출하는
배포에서는 `deploy/livekit.yaml`, `deploy/egress.yaml`, compose의 모든 비밀값을 교체하고
TLS/TURN을 앞단에 구성해야 한다.

## 분석 순서

1. 자녀가 `POST /calls`를 호출하면 동의 상태를 확인하고 LiveKit 룸과 자녀 토큰을 만든다.
2. 등록된 iOS VoIP 토큰이 있으면 APNs PushKit 푸시로 CallKit 수신 화면을 연다.
3. 부모가 `/calls/{id}/accept`하면 부모 토큰과 녹음 asset을 만들고, 이미 publish된 audio
   track과 이후 `track_published`된 audio track에 각각 Track Egress를 시작한다.
4. 부모 앱은 LiveKit `LocalAudioTrack.add(audioRenderer:)`에서 동일 캡처 스트림의 PCM을
   파일로 기록한다. 별도 `AVAudioEngine`으로 마이크를 두 번 열지 않는다.
5. 통화 종료 후 Egress 웹훅과 분석용 PCM 완료 이벤트를 모두 기다린다.
6. 화자별 OGG를 Deepgram Nova-3 한국어 모델에 보내 word/utterance timing과 함께 합친다.
7. 되묻기 규칙 detector를 실행하고, 부모 발화가 20초 이상이면 Gemini가 부모 segment 근거가
   있는 네 항목을 JSON으로 추출한다.
8. word timing과 iOS 16-bit PCM WAV로 음향 4종을 계산하고 calendar-week 기준선·변화
   신호·리포트를 갱신한다.
9. 성공·제외·실패 여부와 관계없이 원본 파일을 폐기하고 폐기 시각을 남긴다.

## iOS 수신 통화

클라이언트 전제는 Swift 네이티브, iOS 우선이다. 상세한 PushKit → CallKit → LiveKit 순서와
분석용 PCM 캡처 방식은 [`docs/ios-call-flow.md`](docs/ios-call-flow.md)에 정리했다.

실기기 수신 통화를 켜려면 Apple Developer에서 VoIP Services가 가능한 App ID와 APNs
토큰 키를 준비한다. Apple Developer Program 팀의 Account Holder 또는 Admin 권한이 필요하다.

1. Certificates, Identifiers & Profiles > Identifiers에서 App ID를 만들고 Bundle ID를
   정한다(예: `com.collog.app`). Capabilities에서 Push Notifications를 켠다.
2. Keys에서 새 key를 만들고 Apple Push Notifications service (APNs)를 체크한다. Configure
   화면의 Environment와 Key Restriction은 **저장 후 변경할 수 없다**. Environment는
   `Sandbox & Production`, Key Restriction은 `Team Scoped (All Topics)`를 권한다. Sandbox
   전용 key로 production endpoint에 보내면 `BadEnvironmentKeyInToken`이 나오고 key를 새로
   발급해야 하는데, APNs key는 팀당 개수 제한이 있다. 받은 `AuthKey_XXXXXXXXXX.p8`은
   **한 번만** 다운로드할 수 있으며 파일명의 10자리가 Key ID다.
3. Team ID는 Membership 화면 우측 상단에서 확인한다.
4. 인증서(Certificates)의 VoIP Services Certificate는 만들지 않아도 된다. 토큰 방식 `.p8`
   하나로 sandbox와 production을 모두 처리한다.

Bundle ID는 팀 전체가 공유하고, `.p8`은 서버 운영자에게만 보안 채널로 전달한다.
값을 받으면 다음을 설정한다.

```dotenv
APNS_VOIP_ENABLED=true
APNS_ENVIRONMENT=sandbox
APNS_TEAM_ID=YOUR_TEAM_ID
APNS_KEY_ID=YOUR_KEY_ID
APNS_BUNDLE_ID=com.example.Collog
APNS_PRIVATE_KEY_PATH=/absolute/or/container/path/AuthKey_XXXX.p8
```

`.p8` 파일은 저장소와 이미지에 넣지 않는다. Compose는 ignored `backend/private/`를
`/run/secrets/collog`에 읽기 전용 mount한다. 파일을 `backend/private/AuthKey_*.p8`에 두고
`APNS_PRIVATE_KEY_PATH`에는 컨테이너 내부의 `/run/secrets/collog/AuthKey_*.p8`를 지정한다.
APNs가 꺼져 있거나 부모 기기에 `voipToken`이 없으면 foreground-only 데모 통화는 계속
동작한다.

Swift 앱이나 실기기 없이도 자격증명만 먼저 검증할 수 있다. 아래는 `.env`의 `APNS_*` 값으로
Apple에 실제 요청을 보내며, `MOCK_EXTERNAL_SERVICES`와 `APNS_VOIP_ENABLED` 값과 무관하게
항상 실제 APNs를 호출한다.

```bash
# 더미 토큰으로 .p8/Team ID/Key ID/Bundle ID 조합만 점검한다.
uv run python -m scripts.check_apns
# 실제 PushKit 토큰으로 CallKit 수신 화면까지 확인한다.
uv run python -m scripts.check_apns --device-token <hex> --environment sandbox
```

더미 토큰 점검에서 `BadDeviceToken`이 나오면 자격증명은 정상이다. Apple이 토큰을 보기 전에
provider JWT와 topic을 먼저 검사하기 때문이다. `InvalidProviderToken`은 `.p8`/Key ID/Team ID
조합 문제, `BadTopic`·`TopicDisallowed`는 Bundle ID 또는 App ID capability 문제다.

Xcode 직접 설치 빌드의 토큰은 `sandbox`, TestFlight/App Store 빌드의 토큰은 `production`
에서만 유효하다. 환경이 어긋나면 같은 토큰이라도 `BadDeviceToken`이 된다.

Gemini는 말하지 않은 원인을 추론하지 않으며 질환명, 위험군 라벨, 응급도, 치료 지시를
생성하지 않도록 시스템 지시와 응답 스키마로 제한한다.

연결 대기 질문은 선택적으로 ElevenLabs에서 한국어 MP3를 생성해 스토리지에 캐시하고,
백엔드가 발급한 만료 URL로 발신자에게만 재생한다. API key는 iOS에 전달하지 않는다.
ElevenLabs가 꺼져 있거나 생성/스토리지 요청이 실패하면 API는 해당 질문에
`ttsMode=IOS_LOCAL`을 반환하고 앱의 `AVSpeechSynthesizer(ko-KR)`로 폴백한다. 따라서 TTS
장애는 통화 생성을 막지 않는다.

```dotenv
QUESTION_TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
ELEVENLABS_MODEL=eleven_flash_v2_5
```

voice 목록 조회와 실제 한국어 MP3 생성 점검:

```bash
uv run python -m scripts.check_elevenlabs
uv run python -m scripts.check_elevenlabs --preview
```

Deepgram Aura TTS는 현재 한국어를 지원하지 않으므로 STT용 Deepgram key를 질문 음성에
재사용하지 않는다.

> Gemini 무료 티어는 해커톤의 더미 데이터에만 사용한다. 무료 티어 입력은 Google 제품
> 개선에 사용될 수 있으므로 실제 건강정보를 처리하는 운영 환경에서는 데이터 비학습 조건의
> 유료 계약 또는 별도의 보호된 모델 엔드포인트로 전환해야 한다.

## 검증

```bash
uv run ruff check .
uv run pytest -q
docker compose config --quiet
```

iPhone 2대 실기기 검증 절차와 자동 판정기는
[`docs/two-iphone-e2e.md`](docs/two-iphone-e2e.md)에 있다.

```bash
# 통화 종료 후 최신 통화를 3분 동안 추적해 Egress→STT→AI-2→폐기를 판정한다.
uv run python -m scripts.verify_two_iphone_call
```

LLM eval은 건강정보가 아닌 고정 더미 fixture만 사용한다.

```bash
uv run python scripts/evaluate_extraction.py --provider mock
# free tier quota에 맞춰 5개씩 나누는 예시
uv run python scripts/evaluate_extraction.py --provider gemini --start 0 --limit 5 --delay 13
```

통화 전체(STT → 가드레일 → 응급 안내 → 제외 판정 → 리포트)는 `evals/call_scenarios.json`의
시나리오 12개로 휴대폰 없이 시험한다. 임시 SQLite DB에서 돌고 끝나면 지운다. 결과 표는
터미널과 `.scenario-results/<시각>.md`에 남고, 실패가 있으면 종료 코드가 1이다.

```bash
# 가짜 모드: 대사를 전사 결과로, fakeFacts를 Gemini 답으로 쓴다. 외부 호출 없음
uv run python -m scripts.run_call_scenarios
# 음성 준비: 새로 만들 문장 수만 세기 → ElevenLabs로 만들기(.scenario-audio/에 캐시)
uv run python -m scripts.scenario_audio --dry-run
COLLOG_TEST_VOICE_PARENT=... COLLOG_TEST_VOICE_CHILD=... uv run python -m scripts.scenario_audio
# 실제 모드: 생성 음성으로 Deepgram·Gemini를 부른다. 키는 환경변수 또는 맥 키체인(-a collog)
COLLOG_TEST_VOICE_PARENT=... COLLOG_TEST_VOICE_CHILD=... \
  uv run python -m scripts.run_call_scenarios --real --only cough
```

테스트는 초대→동의→질환 프로필→통화→이중 업로드→STT/LLM→폐기→리포트 전체 흐름과
Deepgram word timing 정규화, Gemini 부모 근거 semantic validation/키 redaction, 되묻기 규칙,
실제 WAV 음향 4종, calendar-week 기준선, LiveKit Track Egress 요청·웹훅, APNs VoIP 요청
헤더·payload를 포함한다. `evals/extraction_cases.json`의 40개 더미 prompt suite는
`scripts/evaluate_extraction.py`로 mock 또는 실제 Gemini에서 반복 평가한다.

전체 작업 상태와 파일별 역할, 다음 작업은 [`HANDOFF.md`](../HANDOFF.md)를 기준으로 관리한다.
AI-1 prompt/되묻기 설계는 [`docs/ai-transcript-design.md`](docs/ai-transcript-design.md), 실제
음향 지표 설계는 [`docs/acoustic-design.md`](docs/acoustic-design.md)를 기준으로 구현한다.
