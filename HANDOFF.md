# 콜록(Collog) 개발 HANDOFF

마지막 갱신: 2026-10-24 (Asia/Seoul)

이 문서는 콜록 개발의 단일 인수인계 기준이다. 구현, 계약, 검증 결과, 미완료 항목이 바뀌면
코드와 같은 커밋에서 반드시 이 문서를 갱신한다. 비밀키와 실제 건강정보는 기록하지 않는다.

## 0. 팀원·AI 공통 작업 규칙

이 저장소에서 작업하는 사람과 AI는 다른 대화 기록을 전제로 삼지 않는다. `HANDOFF.md`만
읽어도 지금까지의 결정, 구현, 검증, 한계와 다음 작업을 복원할 수 있어야 한다.

작업 시작:

1. **매 턴 어떤 파일도 읽거나 수정하기 전에** `git status -sb`와 `git fetch origin main`을
   실행하고 `git rev-list --count HEAD..origin/main`으로 새 push 여부를 확인한다.
2. 원격이 앞서 있고 working tree가 깨끗하면 `git pull --ff-only`로 최신 `main`을 받는다.
   미커밋 변경이 있으면 pull/덮어쓰기를 하지 말고 충돌 가능성을 먼저 보고한다.
3. 이 문서를 처음부터 끝까지 읽고 관련 코드와 테스트를 확인한다.
4. 이미 완료된 기능을 다시 만들지 않고, `의도적으로 미완료`와 `다음 작업 우선순위`를 기준으로
   작업 범위를 정한다.

작업 종료:

1. 변경한 코드의 lint/test/build를 실행한다.
2. 이 문서의 구현 상태, 파일 역할, 환경변수, 검증 결과, 미완료 항목, 다음 작업, 변경 이력 중
   영향받은 내용을 갱신한다.
3. 새 파일을 만들었으면 `파일별 역할` 표에 반드시 추가한다.
4. API/데이터 흐름/보안 규칙을 바꿨으면 해당 설명과 테스트 결과를 함께 수정한다.
5. 코드와 `HANDOFF.md`를 같은 커밋에 넣고 push한다. 문서만 뒤늦게 맞추는 상태를 만들지 않는다.
6. push 전에 `git diff --check`, secret scan, `git status`를 확인한다.

충돌 또는 불명확한 상태:

- 다른 팀원의 미커밋 파일을 삭제·덮어쓰거나 임의로 되돌리지 않는다.
- 건강 신호의 의미, 개인정보 처리, 외부 API 선택처럼 제품 결정이 필요한 부분은 추측으로
  확정하지 않고 `의도적으로 미완료`에 조건과 질문을 기록한다.
- 실제 키, `.p8`, 토큰, 전화번호, 건강정보를 문서·코드·테스트 fixture에 커밋하지 않는다.

## 1. 우리가 만드는 것

콜록은 가족의 일상적인 통화를 self-hosted LiveKit VoIP로 연결하고, 이번 통화 건강 주체의
사전 동의가 있는 통화만 분석해 개인 내 건강 변화 기록으로 만드는 iOS 우선 앱이다. 부모
건강 관리가 핵심 시연이지만 데이터 모델은 부모·자녀 어느 가족 구성원도 subject가 될 수 있게
일반화한다.

핵심은 진단이 아니라 변화 관찰이다.

- 당사자가 확인한 질환·복용약·걱정 프로필을 바탕으로 다음 통화 질문을 제안한다.
- 부모/자녀 음성을 분리해 Deepgram Nova-3 한국어 STT를 수행한다.
- Gemini가 직접 언급된 증상·복약·활동·수면만 구조화한다.
- 발화 속도, 휴지 비율, 기본주파수 변동, 기침 이벤트를 개인의 같은 시간대 기준선과 비교한다.
- 원본 오디오는 처리 직후 폐기하고 구조화 결과와 특징값만 보관한다.
- 부모와 자녀가 리포트와 가족 공유 범위를 확인할 수 있다.

제품의 절대 금지선은 질환 진단, 위험군 라벨, 응급도 판단, 치료 지시를 모델 출력으로 만드는
것이다. 현재 LLM 시스템 지시와 응답 schema도 이 원칙에 맞춰져 있다.

제품의 중심 데이터 흐름은 `본인 확인 프로필 → 검수된 맞춤 질문 → 통화 자기보고 구조화 →
주·월간 변화 리포트`다. 기침/발화/휴지/F0는 이 흐름을 보조하는 음향 관찰이며 제품의 단독
판단 근거가 아니다. 새 피그마가 부모·자녀 양방향 통화와 양쪽 건강 프로필을 전제로 하므로,
현재 CHILD→PARENT 고정 모델을 일반화하기 위한 계약과 코드 차이는
`backend/docs/profile-question-report-design.md`에 정리했다.

2026-08-13 사용자는 별도로 결정할 수 없는 항목 외에는 권장 기본안을 채택했다. 이후 한 통화의
상대방 한 명만 분석하는 초기안보다 caller/callee 두 참여자를 모두 각자의 건강 subject로
분석하는 안을 선택했다. 연결 질문 target은 callee로 유지한다. 관계 기반 가족 role, 당사자
확인 프로필, 최소 전사 보관, template 질문, observation 기반 리포트 등 확정 기본안과 완료
조건은
`backend/docs/implementation-plan-v2.md`가 실행 기준이다.

필수 분석 동의는 앱 최초 온보딩 완료 조건이다. 정상 통화에서는 양 참여자 분석을 항상
활성화하지만 서버 consent 검증을 제거하지 않는다. 동의 record가 없거나 철회/구버전이면 분석
없는 통화를 허용하지 않고 `CONSENT_REQUIRED`로 온보딩에 돌려보낸다.

## 2. 확정된 기술 전제

| 영역 | 결정 |
|---|---|
| 클라이언트 | Swift 네이티브, iOS 우선 |
| 수신 통화 | APNs VoIP push → PushKit → CallKit |
| 미디어 | LiveKit Cloud가 아닌 self-hosted LiveKit Server |
| 녹음 | LiveKit Track Egress로 부모/자녀 Opus audio track 분리 |
| 분석용 PCM | LiveKit `LocalAudioTrack.add(audioRenderer:)`의 동일 캡처 스트림 |
| API | Python 3.12, FastAPI, async SQLAlchemy |
| DB | 로컬 SQLite, 전체 스택 PostgreSQL |
| 오디오 저장 | 로컬 또는 S3 호환 MinIO |
| STT | Deepgram API, `nova-3`, `language=ko` |
| LLM | Gemini API structured output, 기본 `gemini-3.6-flash` |
| 연결 질문 TTS | backend ElevenLabs Flash v2.5 MP3 생성·캐시·서명 URL. 장애/미설정 시 iOS `AVSpeechSynthesizer(ko-KR)` 폴백 |
| 음향 분석 | Deepgram word timing + `librosa.pyin` + versioned transient 후보 detector |
| 배포 스택 | Docker Compose: backend/Postgres/Redis/MinIO/LiveKit/Egress |

## 3. 현재 구현 상태

### 기능 트랙별 판정

| 트랙 | 판정 | 현재 상태 |
|---|---|---|
| AI-1 Deepgram STT | 연동·실호출 검증 완료 | Nova-3 한국어 요청/응답 정규화 완료. 합성 한국어와 실제 Track Egress OGG를 API로 전사해 E2E 성공 |
| AI-1 LLM 항목 추출(P0-5) | 구현·실호출 검증 완료 | 부모-only segment JSON, polarity/evidence, grounding validator. 실제 처리 7건은 7/7; 나머지 33건은 free-tier quota로 미평가 |
| AI-1 되묻는 표현 탐지 | 구현 완료 | 부모 utterance 한국어 규칙/제외, 3초 병합, rule version, 분당 빈도, transcript/report API |
| AI-2 음향 지표 4종(P0-16) | 부분 동작 | 속도는 실기기 측정됨. 휴지는 항상 0에 가깝고, 기침 detector는 재현율이 사실상 0이며, F0는 유성음 게이트에 걸린다 |
| AI-2 기준선·robust Z(P0-14/P0-6) | 구현·fixture 검증 완료 | ISO calendar-week median, 4개 주, 현재값 제외, MAD=0 UNSCORABLE, 결측 주 연속 중단 |
| 백엔드 P0 API | prototype 완료 | 인증·초대·동의·통화·LiveKit·리포트·정리 loop 구현. 운영용 SMS/worker/migration/실배포 검증은 별도 |

백엔드/AI 해커톤 prototype의 미구현 코드는 크게 줄었고, 2026-08-13에 실기기 통화로
STT/LLM/음향 파이프라인이 끝까지 도는 것을 확인했다. 다만 새 피그마의 양방향 통화·양쪽
건강 프로필·복용약·걱정 항목·근거 기반 리포트 계약은 아직 현재 CHILD→PARENT prototype에
반영되지 않았다. 음향 네 지표 중 실제로 값이 나오는 것은 발화 속도뿐이며, 휴지 비율은
구조적으로 0에 가깝고 기침은 탐지되지 않는다. 어떤 음향 수치도 의료 검증값으로 소개해서는
안 된다. 제품 차이는 `backend/docs/profile-question-report-design.md`, 음향 근거와 보정 계획은
`backend/docs/calibration-todo.md`에 있다.

### 완료

- 원본 명세의 27 paths / 28 operations API 계약
- 개발 OTP와 JWT 인증, 부모 초대, 가족 구성원, append-only 동의, 질환 프로필
- 질환별 오늘의 질문 생성과 최근 질문 중복 회피
- 통화 생성/수락/거절/종료 state flow와 KST time-slot 태깅
- self-hosted LiveKit room/token, 부모·자녀 Track Egress, webhook 서명 검증
- S3/MinIO presigned upload와 로컬 signed upload fallback
- Egress와 분석용 PCM 도착을 모두 기다리는 처리 파이프라인
- Deepgram Nova-3 한국어 STT adapter, utterance/word timing과 segment ID 보존
- Gemini parent-only fact/polarity/evidence JSON, category grounding validator, key header/redaction
- 40개 더미 prompt eval suite와 실제 모델 분할 실행 스크립트
- 한국어 되묻기 규칙·제외·3초 병합, 새 event table과 transcript/report 집계
- 실제 16-bit PCM loader/품질 gate, speech rate/pause/pYIN F0/기침 후보 4종 계산
- 부모 발화 20초 미만 분석 제외
- 성공/제외/실패 모두 원본 오디오 즉시 폐기, 실패 잔존 파일 24시간 purge
- calendar-week anchor/rolling baseline, median/MAD robust z, 결측 주 signal, report snapshot
- iOS PushKit용 APNs HTTP/2 provider와 `/devices` idempotent 등록
- PushKit → CallKit → `/accept` → LiveKit iOS 계약 문서
- `/team` 모바일 웹 포털과 `/team/status.json` 비밀값 없는 연동 상태 endpoint
- Android용 Google 로그인 `/v1/auth/google/challenge`·`/v1/auth/google`. Apple 로그인과 같은
  서버 nonce challenge 1회 소비 구조이며 `users.google_subject`로 계정을 찾는다
- 연결 대기 질문 ElevenLabs provider, MP3 cache, local/S3 만료 download URL과 iOS 로컬 폴백
- ElevenLabs voice 목록/한국어 MP3 preview CLI. provider key는 backend에만 보관
- APNs 자격증명 점검·실기기 발송 CLI와 sandbox 실기기 CallKit 수신 검증
- iOS 앱 골격. PushKit 토큰 발급, VoIP push 수신, CallKit 수신 화면 표시까지 실기기 동작
- iOS 개발 OTP 로그인, 기기 자동 등록, 가족 목록 발신, 발신·수신 공통 통화 화면
- iOS LiveKit room 접속과 CallKit 세션 활성화 후 마이크 publish, remote MP3/iOS 질문 TTS
- iPhone 2대 통화 자동 판정 CLI와 LAN/APNs/고정 대화/장애 분리 E2E 실행서
- iOS 통화 화면의 ElevenLabs/iOS 폴백 badge, remote player 상태·즉시 중단 로그와 전체 로그 복사
- Egress 없는 개발 환경의 부모 PCM-only 분석 mode. `ALLOW_RAW_ONLY_ANALYSIS=true`이면
  `/accept`가 Track Egress 조회·시작을 건너뛰어 부모의 LiveKit token 응답을 지연시키지 않는다.
  운영 기본값은 비활성이며, Egress 호출 0회를 보장하는 회귀 test가 있다.
- 8주 시연용 더미 통화·건강 대화·발화 속도 기준선 seed. `scripts/seed_narrative_history.py`가
  `scripts/data/narrative_history.json`의 주별 내용을 읽어 서로 다른 ISO 주 8개를 만든다.
  ANCHOR(가장 이른 4주)와 ROLLING(최근 4주)이 분리되고 이번 주(W-0)가 포함된다. 실행 시점
  기준 상대 날짜라 재실행 가능하고 `narrative-*` room만 교체한다. `Baseline`/`ChangeSignal`은
  직접 만들지 않고 `SignalService`가 계산한다. 검증되지 않은 기침·휴지·F0는 만들지 않는다.
- 검증되지 않은 지표를 더미로 만들지 않는 seed 정책. 기침·휴지·F0는 생성하지 않고 실측으로
  값이 나오는 발화 속도만 넣는다.
- 리포트 상태 판정에서 채워질 수 없는 기준선을 제외. 부모가 통화한 적 없는 시간대와 측정
  불가능한 지표(미검증 기침 detector)의 `COLLECTING` 행까지 세면 리포트가 영구히 `READY`가
  되지 않는다. 실제로 표본이 들어오는 기준선만 수집 대상으로 본다.
- 리포트 기간 경계를 KST로 계산. 이전에는 `date.today()`(UTC)를 써서 한국 시간 자정~오전
  9시 사이에 이번 주 통화가 통째로 빠졌다.
- 시스템 구성도와 운영비 산정 문서(HTML). 현재 구현 상태와 권장 운영 구성을 구분해 적었고,
  단가는 각 업체 공식 가격표에서 확인한 값만 쓴다.

### 의도적으로 미완료

- **Google 로그인은 `GOOGLE_CLIENT_ID`를 설정해야 켜진다.** 값이 비면 두 엔드포인트가 503이다.
  Google Cloud에 Web OAuth 클라이언트(이 값)와 Android OAuth 클라이언트(패키지 이름·서명
  SHA-1)를 같은 프로젝트에 만들어야 한다. 같은 사람이 iOS(Apple)와 Android(Google)로
  가입하면 계정이 따로 생기며 병합하지 않는다. Google 계정 삭제는 OTP 계정처럼 재인증 없이
  진행한다.
- **Android 통화·리포트 알림은 `FCM_SERVICE_ACCOUNT_PATH`를 설정해야 나간다.** Firebase 서비스
  계정 키 JSON을 컨테이너에 읽기 전용으로 넣고 컨테이너 안 경로를 준다. compose에는 아직 이 키의
  bind가 없으므로 배포할 때 APNs 키처럼 `/run/secrets/fcm.json` bind를 추가해야 한다. 값이 비면
  Android 기기는 수신 기기로 치지 않아 Android만 가진 상대에게 거는 통화는 409(수신 기기 없음)이고,
  리포트 알림도 Android 기기를 대상에서 뺀다. Android 기기는 `platform=ANDROID`로 FCM 토큰 하나를
  `token`·`voipToken`·`pushToken`에 같이 등록한다. 키 JSON은 `.gitignore`가 막는다.
- **새 피그마와 현재 백엔드의 건강 주체 모델이 다르다.** 현재 API는 자녀 발신·부모 분석,
  부모 질환 코드 배열에 고정돼 있다. 부모/자녀 양방향 통화와 양 참여자 동시 분석,
  복용약·걱정·출처·본인 확인, Q/A 관찰 event와 리포트 v2는 설계만 완료됐다.
- **전체 STT segment 보관과 동의 문구가 불일치할 수 있다.** 성공 통화도 `transcripts.segments`에
  전체 전사를 저장하지만 화면은 구조화 항목만 기록한다고 안내한다. 보관 기간과 최소 evidence
  정책을 결정하고 코드/동의 문서를 함께 맞춰야 한다.
- **`COUGH_EVENTS`는 `UNMEASURABLE(DETECTOR_NOT_VALIDATED)`로 고정했다 (v4).**
  `transient-heuristic-v1`이 공개 도메인 라벨 음원에서도 실패했다. 기침 4개 전부 0회,
  8개 파일 최고 점수 0.609로 임계값 0.65에 미달한다. 임계값을 0.40으로 낮추면 웃음이 7회로
  모든 기침 파일보다 높게 잡히므로 상수 튜닝으로는 해결되지 않는다. `0.0 OK`가 기준선에
  쌓이는 것을 막는 것이 이번 변경의 목적이며, **지표를 살리는 작업은 아직 남아 있다.**
  항별 실측 분해는 `backend/docs/calibration-todo.md` 1절에 있다.
- **기침 detector를 HeAR event detector로 교체했다 (v5). 다만 여전히 꺼져 있다.**
  `google/hear`의 `event_detector_small`(MobileNet-V3, 8 class) ONNX 변환본이며 라벨 음원
  8개에서 기침 4/4 검출, hard negative 오탐 0/4다. clip당 약 3 ms, `onnxruntime` 71 MB,
  모델 5 MB로 TensorFlow 없이 돈다. 2초 window가 개별 기침을 분리하지 못해 **단위가 `회`에서
  `구간`으로 바뀌었다.** 켜려면 `cough_detector_validated=true`가 필요하고, 그 전에 기침 30 /
  hard negative 30을 **실제 통화 조건**(부모 발화 + Opus codec + 노년층 화자)에서 측정해야
  한다. 현재 근거는 깨끗한 단일 음원 8개뿐이라 precision을 주장할 수 없다.
- **모델 가중치는 저장소에 없다.** HAI-DEF 약관 대상이라 `backend/models/`를 `.gitignore`에
  넣었다. 각자 `HF_TOKEN=... uv run --with tensorflow --with tf2onnx python
  scripts/fetch_cough_model.py`로 한 번 받는다. 없으면 `MODEL_UNAVAILABLE`, sha256 불일치면
  `MODEL_CHECKSUM_MISMATCH`로 떨어지고 숫자는 만들지 않는다.
- 음향/되묻기 상수를 측정으로 정할 calibration harness가 없다. 라벨 fixture와
  `scripts/calibrate_acoustics.py`가 필요하며 설계는 calibration-todo 2절에 있다.
- `PAUSE_RATIO`는 segment 내부 간격만 세어 항상 0에 가깝다. 지표 정의 변경이라 팀 결정 필요.
- 되묻기 탐지 재현율 미측정. `repeat-ko-v2`에서 `어` 부분 일치 오탐은 고쳤다.
- 전체 40-case 실제 Gemini eval. provider가 처리한 7건은 7/7, 나머지 33건은 free-tier quota로
  요청 자체가 실패해 미평가다.
- **Deepgram Model Improvement Program을 opt-out할지 결정하지 않았다.** 가격표 하단에
  "Rates listed above opt in to the Model Improvement Program"이 있고 MIP 문서는 참여 혜택으로
  "Discounted pricing for program participants"를 든다. 즉 우리가 쓰는 `$0.0048/분`은
  **데이터 학습 이용에 동의한 가격**이다. 현재 `services/deepgram.py`는 `mip_opt_out`을 보내지
  않으므로 부모 건강 대화 오디오가 학습 데이터에 계약상 포함되는 상태다. opt-out은 요청에
  `mip_opt_out=true`를 붙이면 되고 그 경우 데이터는 처리 기간만 보관된다. **다만 opt-out 요율은
  공개되어 있지 않아 비용 영향을 계산할 수 없다.** 실제 사용자 건강정보를 보내기 전에 단가를
  확인하고 코드에 반영할지 결정해야 한다. 해커톤 더미 데이터 구간에서는 문제가 아니다.
- **더미 데이터임을 화면에서 구분할 수 없다.** `seed_narrative_history.py`가 넣은 통화는
  `room_name`이 `narrative-`로 시작하지만 리포트 응답에는 더미 여부를 알리는 필드가 없다.
  데모에서 실제 측정값처럼 보이지 않게 하려면 응답에 표시를 넣고 리포트 화면에 함께 붙여야 한다.
- **seed는 데모 당일 아침에 다시 실행한다.** 상대 날짜로 만들지만 며칠이 지나면 가장 이른 주가
  ROLLING 창 밖으로 밀리고 W-0이 비어 `COLLECTING`으로 떨어진다. 재실행은 `narrative-*`만
  교체하므로 안전하다.
- **acute(vs_rolling) signal은 주 1회 통화로는 구조적으로 나오지 않는다.**
  `process_call()`이 현재 통화를 자기 기준선에서 제외하는데 ROLLING 창은 4주(W-3~W-0)이고
  `baseline_required_samples`도 4다. 주 1회면 현재 통화를 빼면 3표본이라 ROLLING이
  `COLLECTING`으로 떨어지고 `vs_rolling`이 `None`이 된다. 8주 seed도 `acuteSignals`가 0건이며
  이는 버그가 아니다. 같은 주에 통화가 2건이면 두 번째 통화에서 ROLLING이 4표본이 되어 계산된다.
  데모에서 acute까지 보여야 하면 W-0 항목을 하나 더 넣고 값을 216 아래로 둔다.
  **지표 정의 변경이므로 코드는 건드리지 않고 결정 사항으로 남긴다.**
- **seed는 spec의 time slot 하나에만 기준선을 만든다.** 현재 spec은 전부
  `AFTERNOON_EVENING`이므로 데모 통화도 같은 시간대여야 한다. 다른 시간대에 통화하면 그 slot에는
  기준선이 없어 변화 signal이 나오지 않는다. `scripts/data/narrative_history.json`의 `timeSlot`을
  바꾸거나 데모 시간대를 맞춘다. 실제 통화의 시간대는 `api.py`가 KST 6~11시면 `MORNING`,
  그 밖이면 `AFTERNOON_EVENING`으로 태깅한다.
- SMS OTP 실제 발송 provider. 개발 OTP는 `000000`이다.
- ElevenLabs 실제 key/voice ID를 `.env`에 넣어 한국어 음색을 선택하고, 실기기에서 remote MP3
  재생과 상대 수락 즉시 중단을 확인해야 한다. 생성·storage 실패 시 local TTS 폴백은 test 완료다.
- 실기기 양단 통화. 2026-08-13에 한 대(부모 역할)로 push→수락→LiveKit→PCM 업로드→분석까지
  검증했고 자녀 쪽은 API로 대신했다. iPhone 2대로 실제 음성이 오가는 통화는 아직이다.
- 현재 발급한 `.p8`는 **sandbox 전용**이다. production endpoint는 `BadEnvironmentKeyInToken`
  으로 거부된다. Xcode 직접 설치 빌드 데모에는 문제가 없지만 TestFlight/App Store 빌드로
  넘어가려면 Sandbox & Production key를 새로 발급해야 한다.
- 통화 시작 직후 구간이 분석용 PCM에서 유실될 가능성. writer가 `마이크 publish` 이후에
  부착되므로 받자마자 말하면 앞부분이 빠질 수 있다. 재현 확인이 필요하다.
- iOS 앱의 초대·동의·질환 프로필 화면. 부모 계정은 백엔드 API로 먼저 만들어야 하며
  앱에서는 로그인과 수신만 가능하다. 2026-08-15에 홈 대시보드·리포트·통화 탭은 붙었지만
  피그마의 첫 화면/온보딩 3장/로그인(카카오)/동의 확인/본인 프로필 생성/부모 초대/초대
  확인/초대 수락 화면은 아직 없다.
- **iOS 타임라인 탭은 빈 화면이다.** 서버에 통화별 health observation 타임라인 endpoint가
  없어서(implementation-plan-v2 Phase 5) 데이터를 지어내지 않고 준비 중 상태만 표시한다.
- **홈 히어로의 `+N일차`는 마지막 분석 완료 통화 기준이다.** 피그마의 `전화 주기가 도래했어요`가
  전제하는 사용자별 목표 통화 주기 설정이 서버에 없다. 주기 설정을 넣기 전까지는 경과일만
  보여주고 `도래` 판정은 하지 않는다.
- iOS 토큰 저장은 `UserDefaults`다. 실사용 배포 전 Keychain으로 옮긴다.
- iOS 통화 화면의 실기기 검증. 빌드와 계약은 맞췄지만 양단 통화, 오디오 라우팅,
  질문 TTS 즉시 중단은 실제 iPhone 2대로 확인해야 한다.
- APNs 토큰 410/Unregistered 시 DB device 비활성화. 현재 발송 실패를 로그로 남긴다.
- 내구성 있는 작업 큐. 현재 분석 재시도/정리는 FastAPI process의 background task다.

## 4. 전체 통화 흐름

```text
자녀 iOS             Backend               self-hosted LiveKit       부모 iOS
   │ POST /calls        │                          │                    │
   ├───────────────────>│ consent/profile 검증    │                    │
   │                    ├─ create room/token ─────>│                    │
   │<─ token/questions ─┤                          │                    │
   │                    ├─ APNs VoIP push ─────────────────────────────>│
   │ ElevenLabs/로컬 TTS│                          │       PushKit→CallKit
   │ join/publish ────────────────────────────────>│                    │
   │                    │<──────── POST /accept ────────────────────────┤
   │                    ├─ parent token + 기존 child track 조회        │
   │                    │<─ signed track_published webhook ─────────────┤
   │                    ├─ per-track OGG Egress ──>│                    │
   │<══════════════════════ WebRTC audio ═══════════════════════════════>│
   │                    │                          │  PCM renderer 기록 │
   │ POST /end ────────>│ stop Egress             │<─ upload PCM ──────┤
   │                    │<─ egress_ended webhook ─┤                    │
   │                    ├─ STT → repeat/LLM → acoustics → report       │
   │                    └─ 모든 원본 오디오 purge                      │
```

APNs payload에는 `callId/callUUID/callerId/callerName/expiresAt`만 넣는다. LiveKit 토큰과
건강정보는 넣지 않는다. 부모의 인증된 `/accept` 응답에서만 부모 LiveKit 토큰을 발급한다.

## 5. 파일별 역할

### 저장소 루트

| 파일 | 역할 |
|---|---|
| `README.md` | 프로젝트 입구와 주요 문서 링크 |
| `HANDOFF.md` | 구현/결정/검증/다음 작업의 단일 기준 문서 |
| `.gitignore` | 키, 로컬 DB·오디오, venv, cache 제외 |

### backend 핵심

| 파일 | 역할 |
|---|---|
| `backend/app/main.py` | FastAPI 생성, lifespan, CORS, 오류 응답, 정리 loop |
| `backend/app/api.py` | 28개 REST endpoint와 통화 orchestration |
| `backend/app/config.py` | 환경변수와 provider 설정 |
| `backend/app/container.py` | DB/storage/LiveKit/STT/LLM/APNs/pipeline 의존성 조립 |
| `backend/app/team_portal.py` | 모바일/WebView 팀 포털 HTML과 비밀값 없는 provider 상태 snapshot |
| `backend/app/database.py` | async engine/session/Base |
| `backend/app/schema_guard.py` | 기동 시 모델과 DB 스키마 비교. 로컬은 재생성, 배포는 기동 거부 |
| `backend/app/models.py` | 사용자, 가족, 동의, 통화, 오디오, 추출, 기준선, signal, report DB 모델 |
| `backend/app/schemas.py` | camelCase API request/response와 Gemini 추출 schema |
| `backend/app/security.py` | OTP hash, JWT 발급/인증, role 검사 |

### backend provider 및 도메인 서비스

| 파일 | 역할 |
|---|---|
| `services/livekit.py` | self-host LiveKit room/token/audio-track 조회/Track Egress/webhook adapter와 mock |
| `services/fcm.py` | Android FCM HTTP v1 발송. 서비스 계정 JWT로 OAuth 토큰 발급·재사용, 수신 통화·리포트 데이터 메시지, 만료 토큰 판정 |
| `services/google_auth.py` | Google ID 토큰 JWKS 검증(iss 두 값, aud=Web client ID, nonce), 키 cache·실패 backoff |
| `services/notifications.py` | APNs ES256 provider JWT와 HTTP/2 VoIP push, mock/disabled adapter |
| `services/storage.py` | 로컬/S3 저장, presigned upload/download, TTS write/cache/read/delete |
| `services/tts.py` | ElevenLabs 한국어 질문 합성·cache key·만료 URL과 질문별 iOS 폴백 |
| `services/deepgram.py` | Nova-3 STT와 utterance/word timing/segment 정규화 |
| `services/gemini.py` | 부모-only fact/polarity/evidence output와 semantic validator |
| `services/repeat_detector.py` | 설명 가능한 한국어 되묻기 규칙·제외·3초 event 병합 |
| `services/acoustics.py` | PCM loader/품질 gate/속도/휴지/pYIN F0/transient 기침 후보 분석 |
| `services/pipeline.py` | 입력 대기→STT→LLM→음향→signal→purge orchestration |
| `services/signals.py` | 주별 anchor/rolling 기준선, MAD 비교, 결측 주 연속 signal 계산 |
| `services/reports.py` | 주간/월간 report와 되묻기 관찰 snapshot 생성. 기간 경계는 KST, 조회는 UTC |
| `services/questions.py` | 질환별 질문 pool과 선택. TTS provider 적용 전 기본 local mode |
| `services/domain.py` | 가족 접근, 동의, 초대 상태 공통 규칙 |
| `services/http.py` | Deepgram/Gemini transient retry helper |

### 배포·문서·테스트

| 파일 | 역할 |
|---|---|
| `backend/docker-compose.yml` | 전체 self-hosted 개발 스택 |
| `backend/deploy/livekit.yaml` | LiveKit/Redis/webhook/port 설정. 로컬 LAN용(`use_external_ip:false`) |
| `backend/deploy/livekit-cloud.yaml` | 공인 IP 뒤 클라우드용. `use_external_ip:true` 하나만 다르다 |
| `backend/deploy/egress.yaml` | Egress worker/MinIO 설정 |
| `backend/Dockerfile` | API와 실기기 preflight/검증 CLI가 포함된 container image |
| `backend/.env.example` | 비밀값 없는 환경변수 template |
| `backend/private/README.md` | ignored APNs `.p8`의 local Compose read-only mount 위치 안내 |
| `backend/docs/ios-call-flow.md` | Swift PushKit/CallKit/LiveKit/PCM 구현 계약 |
| `backend/docs/two-iphone-e2e.md` | iPhone 2대 LAN/APNs/LiveKit/AI 전체 E2E 체크리스트와 장애 분리 |
| `backend/docs/cloud-deploy.md` | 클라우드 배포 절차와 사전 검증. 인스턴스/보안 그룹/공인 IP 환경변수/확인 순서 |
| `backend/docs/ai-transcript-design.md` | LLM 위치/prompt v2/eval과 되묻기 규칙 detector 설계 |
| `backend/docs/acoustic-design.md` | 음향 4종 정의, 품질 gate, model, worker와 검증 기준 |
| `backend/docs/voice-health-model-research.md` | HeAR/기침 detector 판정, 유사 서비스 비교, 되묻기·난청 한계와 검증 설계 |
| `backend/docs/profile-question-report-design.md` | 피그마 기준 건강 프로필→질문→통화 자기보고→주·월간 리포트 계약과 현재 코드 차이 |
| `backend/docs/implementation-plan-v2.md` | 승인된 권장 기본값, Phase 0~7 구현 순서·완료 조건, 실제 질문이 필요한 외부 조건 |
| `backend/docs/schema-management-design.md` | 로컬 schema guard와 배포 Alembic의 두 축, 판정 규칙, 가비아 서버 배포 제약 |
| `backend/docs/service-proposal-outline.md` | 초기 서비스 기획안을 개조식으로 재구성하고 현재 제품·구현·검증 상태에 맞춰 정정한 문서 |
| `output/pdf/collog-service-proposal-outline.pdf` | 팀 공유·검토용 개조식 서비스 기획안 PDF 산출물 |
| `backend/docs/system-architecture.html` | 구성 요소·포트·통화/분석 흐름·원본 폐기 구성도. 현재 구현과 권장 운영 구성을 구분 |
| `backend/docs/operating-cost.html` | 운영비 산정 정본. 공식 단가와 출처, Deepgram 과금 구조·무료 크레딧·계정 정책, 규모별 provider 내역, 감도 분석, 계산식 |
| `output/html/collog-operating-cost.html` | 팀 공유·검토용 결론 문서. 규모별 provider 비용표를 맨 앞에 두고 무료 크레딧·계정 질문·주의사항만 남긴 축약본. 외부 asset 없이 단독 실행 |
| `backend/evals/extraction_cases.json` | parent/child/부정/정정/injection 40개 더미 LLM fixture |
| `backend/scripts/evaluate_extraction.py` | mock/Gemini fixture 평가, 분할/지연 실행 CLI |
| `backend/scripts/check_apns.py` | APNs 자격증명 점검과 실기기 VoIP push 발송 CLI |
| `backend/scripts/check_elevenlabs.py` | ElevenLabs voice 목록 조회와 한국어 질문 MP3 preview CLI |
| `backend/scripts/preflight_two_iphone.py` | LAN 주소/provider/APNs/MinIO의 통화 전 비밀값 없는 사전 판정 |
| `backend/scripts/verify_two_iphone_call.py` | 최신/지정 통화의 양 Track Egress, 양 화자 STT, AI-2, purge 자동 판정 |
| `backend/scripts/seed_demo_family.py` | 개발 OTP로 자녀-부모 초대·수락·동의·질환 프로필 생성 CLI |
| `backend/scripts/data/narrative_history.json` | 8주 데모 seed의 주별 내용. `weekOffset`/`timeSlot`/`askedQuestionIds`/`speechRate`/`selfReport` |
| `backend/scripts/seed_narrative_history.py` | 파이프라인을 건너뛰고 지정한 값으로 8주 이력을 넣는 CLI. 기준선·신호는 `SignalService`가 계산 |
| `backend/scripts/seed_mock_history.py` | 음성 합성부터 실제 파이프라인을 태워 이력을 만드는 CLI. 값 지정 불가, 외부 API 할당량 소모 |
| `backend/scripts/replay_call.py` | Egress 없이 로컬 오디오로 STT/LLM/음향 파이프라인 실행 CLI |
| `backend/scripts/acoustic_quality_report.py` | 음향 지표 측정 성공률과 실패 사유 분포 집계 CLI |
| `backend/deploy/livekit-local.yaml` | Docker 없이 실행하는 단일 노드 LiveKit 설정. Egress 없음 |
| `backend/docs/calibration-todo.md` | 보정되지 않은 상수와 측정 계획. 근거와 완료 기준 |
| `backend/tests/test_api_flow.py` | 온보딩부터 분석·폐기·리포트까지 E2E API test |
| `backend/tests/test_providers.py` | Deepgram/Gemini/APNs provider unit test |
| `backend/tests/test_ai_pipeline.py` | prompt/repeat/acoustic/calendar-week deterministic test |
| `backend/tests/test_tts.py` | ElevenLabs 요청 계약, cache/서명 URL, 장애 local 폴백 test |
| `backend/tests/test_schema_guard.py` | 스키마 판정, ADDITIVE 데이터 보존, DRIFTED 재생성, 배포 기동 거부 test |
| `backend/tests/conftest.py` | 격리 SQLite와 mock provider test app fixture |
| `backend/pyproject.toml` | Python 의존성, ruff/pytest/build 설정 |
| `backend/uv.lock` | 재현 가능한 dependency lock |

### iOS 앱

| 파일 | 역할 |
|---|---|
| `ios/Collog.xcodeproj` | Xcode project. Bundle ID `com.Collog`, deployment target iOS 17.0, LiveKit SPM |
| `ios/Collog/CollogApp.swift` | `AppDelegate`로 실행 초기 PushKit/APNs 등록과 remote token 수신 |
| `ios/Collog/VoipCallCenter.swift` | PushKit 수신, CallKit 발신/수신 action, LiveKit room 접속과 마이크 publish |
| `ios/Collog/AnalysisPCMWriter.swift` | LiveKit local capture를 callback 수명 안에서 48 kHz mono int16 WAV로 기록 |
| `ios/Collog/CollogAPI.swift` | OTP/기기/가족/통화 REST client와 서버 오류 message 파싱 |
| `ios/Collog/AppSession.swift` | 로그인 세션·가족 구성원 상태. 토큰과 backend URL을 UserDefaults에 보관 |
| `ios/Collog/RingingQuestionSpeaker.swift` | ElevenLabs remote MP3 상태 관찰·실패 local 폴백·즉시 중단 로그 |
| `ios/Collog/ContentView.swift` | 로그인 분기와 통화 화면 overlay. 로그인 후 화면은 `RootTabView`가 소유 |
| `ios/Collog/ColloTheme.swift` | 피그마 `일하자`(88:516) Foundation/Typography 토큰. 색·타입 스케일·radius·spacing |
| `ios/Collog/RootTabView.swift` | 피그마 하단 5탭(홈/통화/리포트/타임라인/설정) 셸과 리포트·설정 탭 |
| `ios/Collog/HomeDashboardModel.swift` | 홈 대시보드용 subject 선택과 리포트/질문/통화기록 병렬 로드 |
| `ios/Collog/HomeDashboardView.swift` | 피그마 `홈 대시보드 화면`(98:21586) 구현과 서버 관찰값 바인딩, 추이 그래프 |
| `ios/Collog/LoginView.swift` | 개발 OTP 로그인과 backend base URL 입력 |
| `ios/Collog/CallView.swift` | 발신·수신 통화 화면. 오늘의 질문, ElevenLabs/폴백 badge, 종료 버튼 |
| `ios/Collog/Assets.xcassets/HomeCharacter.imageset` | 홈 히어로 캐릭터. 피그마 export PNG 원본 바이트 |
| `ios/Collog/Assets.xcassets/IconMyHealth.imageset` | 「우리 가족 건강」 멀티컬러 아이콘(92:990) 피그마 export SVG |
| `ios/Collog/Collog.entitlements` | `aps-environment` push entitlement |
| `ios/Collog/Info.plist` | `voip`/`audio`, 마이크·LAN 설명과 개발용 local-network ATS 허용 |

발신도 CallKit `CXStartCallAction`을 거친다. 그래야 `didActivate`에서 오디오 세션을 받는
경로가 발신·수신 모두 같아진다. LiveKit `isAutomaticConfigurationEnabled`는 꺼져 있고
엔진은 `.none`으로 시작하므로, CallKit이 세션을 활성화하기 전에는 오디오 장치를 잡지 않는다.
room 접속과 마이크 publish는 분리되어 있고 publish는 `didActivate` 이후에만 실행한다.

`project.pbxproj`에 `DEVELOPMENT_TEAM`이 고정되어 있다. 같은 Apple Developer 팀에 초대되지
않은 팀원은 Signing & Capabilities에서 자기 팀으로 바꿔 빌드하고 그 변경은 커밋하지 않는다.
Xcode가 만드는 `ios/.git`은 제거했다. 다시 생기면 저장소가 iOS 소스 대신 submodule 링크만
기록하므로 clone한 팀원에게 빈 `ios/`가 전달된다.

## 6. 실행과 검증

API만 실행:

```bash
cd backend
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --port 8080
```

검증:

```bash
cd backend
uv run ruff check .
uv run pytest -q
uv build
docker compose config --quiet
```

2026-08-13 `feat/ios-pushkit` 통합 검증 결과:

- `uv run ruff check .`: 통과
- `uv run pytest -q`: 47 tests 통과, FastAPI TestClient의 upstream deprecation warning 1개
- `uv build`: wheel/sdist 생성 성공
- `docker compose config --quiet`: 통과. analyzer v3/품질 gate/raw-only 환경변수 전달 확인
- `fix/skip-track-egress-raw-only` main 통합: `uv run ruff check .` 통과, `uv run pytest -q`
  47 tests 통과. raw-only `/accept`가 Track Egress 조회·시작을 호출하지 않는 회귀 assertion 추가
- Swift 전체 source `swiftc -frontend -parse`: 문법 검사 통과
- `plutil -lint`: Xcode project, Info.plist, entitlement 통과
- 이 Mac은 full Xcode가 아닌 Command Line Tools만 활성화되어 있어 이번 main 통합 시점에는
  `xcodebuild`를 재실행하지 못했다. 브랜치 작성자는 같은 소스를 Xcode/실기기에서 빌드해
  APNs sandbox CallKit 수신과 부모 PCM 분석을 확인했다고 기록했다.
- generated OpenAPI: 27 paths / 28 operations

2026-08-14 `feat/schema-guard` 검증 결과:

- `uv run ruff check .`: 통과
- `uv run pytest -q`: 61 tests 통과 (기존 47 + schema guard 14). warning은 기존 FastAPI
  TestClient deprecation 1개로 변동 없음
- `uv build`: wheel/sdist 생성 성공
- `docker compose config --quiet`: 통과. `SCHEMA_AUTO_RESET` 전달 확인
- Postgres 17 실측 통과: 같은 13개 test를 `GUARD_TEST_DATABASE_URL`로 `postgres:17-alpine`에
  대해 실행해 전부 통과. 외래키 제약 20개가 걸린 상태에서 `DROP TABLE users`는
  `cannot drop table users because other objects depend on it`으로 거부되지만 reflect 기반
  정렬 drop은 성공하므로, SQLite가 검증하지 못하는 삭제 순서가 실제로 확인됐다. `MATCHED`
  재실행 무동작과 `SCHEMA_AUTO_RESET=false` 정상 통과도 같은 DB에서 확인. 절차는
  `backend/docs/schema-management-design.md` 8-1절
- 미완료: Alembic 도입과 4-5절 배포 리허설
- `docker-compose.yml`, `deploy/livekit.yaml`, `deploy/egress.yaml`: YAML parse 통과
- Docker Compose 5.4.0 + Colima arm64에서 Postgres/Redis/MinIO/LiveKit/Egress/backend 전체
  stack을 새 librosa/numpy image로 재빌드·기동하고 health/container import 확인
- 기존 PostgreSQL volume에 새 `repeat_events`, `extraction_evidence`,
  `acoustic_analysis_runs` table 자동 생성 확인
- Deepgram 실제 key: macOS 한국어 합성 WAV를 `nova-3`, `language=ko`로 전사 성공
- Gemini 실제 key: `gemini-3.6-flash` structured JSON 추출 성공
- Gemini prompt v2 실제 더미 중 provider 처리 7건은 7/7. 나머지 33건은 13초 간격에도
  free-tier quota로 요청 실패하여 전체 40건 점수로 기록하지 않음
- 동일 40-case suite의 deterministic mock regression은 40/40 통과
- 실제 iOS 형식 48 kHz PCM resample/sine/known pause fixture에서 음향 4종 OK, 16 kHz 분리
  transient 2개를 기침 후보 2개로 계산. invalid WAV/sample-rate/MAD=0/결측 주 test 통과
- 실제 통합 smoke: LiveKit CLI로 자녀 30.4초/부모 42.4초 Opus track publish → 분리 Track
  Egress → MinIO → Deepgram(부모 발화 42초, 23 segments) → Gemini(4개 항목 모두 존재) →
  `ANALYZED` → 원본 3개 asset purge까지 성공
- `/team` Docker HTML 렌더와 `/team/status.json`을 localhost 및 LAN 주소에서 확인. 실제 key는
  노출하지 않고 provider별 configured boolean만 반환
- ElevenLabs 실제 `sk_` key/선택 voice로 `eleven_flash_v2_5`, `language_code=ko` MP3 생성
  성공. Docker 질문 API가 `ttsMode=REMOTE_ASSET`과 MinIO 서명 URL을 반환하고 해당 URL에서
  `200 audio/mpeg`(51,035 bytes) 수신 확인
- `scripts.preflight_two_iphone`을 Docker 이미지에서 실행 확인. 이 workstation의 현재 `.env`는
  Deepgram/Gemini/JWT/LiveKit/MinIO/ElevenLabs가 통과하며, iPhone용 LAN URL 3개와 APNs
  Team/Key/Bundle ID·`.p8`는 아직 미설정이다. 이를 채우기 전 잠금화면 양단 테스트는 불가하다.
- ElevenLabs key ID 오입력의 `400 invalid_api_key`를 확인해 provider/Team Hub/preflight가
  `sk_` 실제 key 형식을 구분하도록 보강

2026-08-20 저장소 이전과 브랜치 통합 검증 결과:

- 저장소를 `Frekion2002/14th-hackathon`에서 `Collog-App/14th-hackathon`으로 전송했다.
  브랜치 8개와 PR #1이 보존되고 구 URL은 GitHub이 리다이렉트한다.
- `fix/pipeline-report-and-auth-gates`를 main에 통합했다. `reports.py`가 유일한 충돌이었고
  브랜치 쪽을 채택했다. 중복이던 seed 구현(`services/demo_seed.py`,
  `app/data/demo_history.json`, `scripts/seed_demo_history.py`, `tests/test_demo_history.py`)은
  삭제했다. 같은 일을 하는 `scripts/seed_narrative_history.py`가 남는다.
- `uv run ruff check app scripts tests`: 통과. 통합 직후에는 `pipeline.py` E501과
  `seed_narrative_history.py` ASYNC240 두 건이 걸려서 각각 줄바꿈과 동기 함수 분리로 고쳤다.
- `uv run pytest -q`: 64 tests 통과. 파일별로 `test_ai_pipeline` 33, `test_schema_guard` 14,
  `test_api_flow` 8, `test_providers` 6, `test_tts` 3이다. 삭제한 `test_demo_history` 6건이
  빠져 70 &rarr; 64가 됐다.
- `docker compose config --quiet`: 통과.
- **PostgreSQL 실측**: backend를 재빌드한 뒤
  `scripts.seed_demo_family` &rarr; `scripts.seed_narrative_history` &rarr;
  `GET /v1/parents/{id}/reports?period=WEEKLY`를 실제 HTTP로 완주했다. 통화 8건이 전부
  `AFTERNOON_EVENING`으로 들어가고 발화 속도는 268/259/273/262 &rarr; 245/238/229/221이다.
  리포트는 `HTTP 200`, `state=READY`, 승격 signal 1건
  (`발화 속도 4주 연속 변화, 처음 대비 -17%`), `acuteSignals` 0건이다.
- **정정**: 2026-08-17 항목의 "리포트 저장 실패 잠재 버그"는 실재하지 않았다. `jsonable_encoder`
  없이도 `observedAt` datetime이 들어간 snapshot이 PostgreSQL에 정상 저장되고 API가 200을
  반환하는 것을 확인했다. 그 수정은 브랜치 통합 과정에서 함께 사라졌고 되살릴 이유가 없다.
- **주의**: 이 실측은 이 workstation의 Compose stack이며 가비아 서버가 아니다. 가비아에서는
  `SCHEMA_AUTO_RESET=false`라 빈 DB 첫 기동이 거부되므로 `cloud-deploy.md` 6절 절차를 따른다.
- **주의**: seed를 고친 뒤 `docker compose up -d --build backend`로 재빌드하지 않으면 컨테이너는
  옛 코드로 돈다. 2026-08-18에 5일 전 image가 새 파일 없이 돌던 것을 확인했다.

현재 이 Mac에는 Homebrew `docker-compose 5.4.0`, `docker-buildx 0.36.1`,
`livekit-cli 2.18.2`가 설치되어 있고 `~/.docker/config.json`의 `cliPluginsExtraDirs`가
`/opt/homebrew/lib/docker/cli-plugins`를 가리킨다. 이는 machine-local 설정이라 Git에는 없다.
전체 stack은 `backend/`에서 실행 중이다. 상태는 `docker compose ps`, 중지는
`docker compose down`으로 수행하되 DB/MinIO volume을 보존하려면 `-v`를 붙이지 않는다.

Gemini thinking-capable Flash 모델은 내부 추론 토큰도 output budget을 사용한다. 500으로 두면
짧은 JSON도 `MAX_TOKENS`로 잘렸기 때문에 기본 `GEMINI_MAX_OUTPUT_TOKENS=2048`을 사용하고,
`finishReason != STOP` 응답은 불완전 JSON으로 파싱하지 않고 명시적으로 실패시킨다.
API key는 query string이 아니라 `x-goog-api-key` header로 보내 exception URL/로그 노출을 막고,
legacy query-key 형태의 예외 문자열도 redaction한다.

화자 분리 OGG는 반드시 Track Egress로 저장한다. Participant Egress는 오디오·비디오
transcode 경로여서 이 stack에서 OGG와 호환되지 않았다. `/accept`는 이미 publish된 자녀
microphone track을 조회하고, 이후 부모 track은 `track_published` webhook에서 시작한다.

Swagger는 `http://localhost:8080/docs`, health는 `GET /v1/health`다.
팀 포털은 `http://<backend-host>:8080/team`, machine-readable 상태는
`GET /team/status.json`이다. Swift에서는 같은 URL을 `WKWebView` 최상위 navigation으로 열 수
있다. 포털은 키 값이 아닌 configured boolean만 표시하지만, 인터넷에 직접 노출하는 production
환경에서는 `TEAM_PORTAL_ENABLED=false`로 끄거나 인증/TLS 앞단을 둔다.

전체 스택은 `.env`에 Deepgram/Gemini 키와 iPhone에서 접근 가능한 LAN 주소를 넣은 뒤
`docker compose up --build`로 시작한다. ElevenLabs 질문 음성을 쓰면 key와 voice ID도 넣는다.
APNs는 선택 사항이며 `.p8`를 절대 커밋하지 않는다.

Compose는 ignored `backend/private/`를 `/run/secrets/collog`에 read-only mount한다. APNs key는
`backend/private/AuthKey_*.p8`에 두고 `.env`에는 컨테이너 내부 경로인
`APNS_PRIVATE_KEY_PATH=/run/secrets/collog/AuthKey_*.p8`를 설정한다.

## 7. 보안·데이터 불변조건

- 최신 부모 동의가 `GRANTED`가 아니면 Egress와 PCM 업로드를 시작하지 않는다.
- 동의 이력은 overwrite하지 않고 append-only로 쌓는다. 애플리케이션은 어떤 경우에도
  동의 record를 수정하지 않는다. 단 `SCHEMA_AUTO_RESET=true`인 **로컬 개발 DB**는 스키마가
  어긋날 때 통째로 재생성되므로 이력이 유지되지 않는다. 대상은 더미 데이터이며, 배포
  환경은 `SCHEMA_AUTO_RESET=false`로 이 경로가 차단된다.
- 부모 발화 20초 미만은 LLM/음향 변화 분석에서 제외한다.
- 원본 오디오는 분석 성공 여부와 관계없이 폐기한다.
- 저장 가능한 것은 구조화 텍스트, 파생 특징값, 리포트다.
- Gemini 무료 티어에는 해커톤 더미 데이터만 보낸다.
- Deepgram 요청도 같은 종류의 조건이 걸려 있다. `$0.0048/분`은 Model Improvement Program에
  opt-in한 가격이며 현재 코드는 opt-out하지 않는다. 실제 건강정보를 보내려면 `mip_opt_out=true`
  적용 여부와 그때의 단가를 먼저 확정한다.
- LLM은 대화에 없는 원인, 질환, 위험도, 응급도, 치료를 생성하지 않는다.
- 비교는 인구집단 진단 cutoff가 아니라 같은 사람·같은 time slot의 이전 기록을 기준으로 한다.
- Apple `.p8`, Deepgram/Gemini/API/JWT secret은 Git에 넣지 않는다.

### 자격증명 구분

| 자격증명 | 어디서 얻는가 | 해커톤 필요 여부 | 용도 |
|---|---|---|---|
| `DEEPGRAM_API_KEY` | Deepgram Console 발급 | 필수 | Nova-3 한국어 STT |
| `GEMINI_API_KEY` | Google AI Studio 발급 | 필수 | 구조화 LLM. OpenAI key와 동시에 필요하지 않음 |
| `ELEVENLABS_API_KEY`/voice ID | ElevenLabs 발급·Voice Library 선택 | 서버 질문 음성 사용 시 필수 | 연결 대기 한국어 MP3. iOS에는 전달하지 않음 |
| `HF_TOKEN` | Hugging Face 계정에서 HAI-DEF 약관 수락 후 발급 | HeAR detector artifact를 처음 받을 때만 필요 | gated model download. 앱/런타임 API key가 아니며 Git에 넣지 않음 |
| `LIVEKIT_API_KEY/SECRET` | 우리가 직접 강한 난수로 생성 | 필수 | self-hosted room token, server API, Egress, webhook 서명 |
| `JWT_SECRET` | 우리가 직접 강한 난수로 생성 | 필수 | 콜록 사용자 인증 JWT |
| `FCM_SERVICE_ACCOUNT_PATH` | Firebase Console 프로젝트 설정의 서비스 계정 키(JSON) | Android 통화 수신 시 필수 | FCM 발송. 비밀값이며 Git에 넣지 않음 |
| `GOOGLE_CLIENT_ID` | Google Cloud Console의 Web OAuth 클라이언트 ID | Android Google 로그인 시 필수 | ID 토큰 `aud` 검증. 비밀값 아님 |
| APNs `.p8`/Key ID/Team ID/Bundle ID | Apple Developer 발급·확인 | 실기기 백그라운드 수신 시 필수 | PushKit VoIP push |
| MinIO access key/secret | 우리가 직접 생성 | Egress 녹음 시 필수 | self-hosted S3 호환 오디오 저장 |

최소 foreground 데모에서 외부 업체로부터 받을 것은 Deepgram key와 Gemini key 두 개다.
ElevenLabs 음색을 쓰면 ElevenLabs key와 voice ID가 추가된다.
LiveKit key/secret은 LiveKit Cloud에서 받지 않는다. 실제 iOS PushKit 수신까지 시연하면 Apple
APNs 자격증명이 추가된다. Gemini 무료 tier에는 제품 개선 데이터 사용 조건이 있으므로 실제
건강정보가 아닌 더미 데이터만 사용한다.

Deepgram도 같은 조건이 있다. 공개 가격표의 `$0.0048/분`은 Model Improvement Program opt-in
요율이고, opt-out(`mip_opt_out=true`) 시의 요율은 공개되어 있지 않다. 실제 건강정보를 보내기
전에 확인한다. Deepgram은 구독이 아니라 초 단위 pay-as-you-go 크레딧이며 신규 계정 $200 무료
크레딧은 만료가 없다. 우리 요율로 41,667분이라 100가구 사용량(월 9,600분) 기준 약 4.3개월치다.

Deepgram Aura TTS는 2026-08-11 공식 지원 언어에 한국어가 없어 사용하지 않는다. ElevenLabs
`eleven_flash_v2_5`, `language_code=ko`, 기본 MP3 44.1 kHz/128 kbps를 사용한다. 생성물은 질문
ID+voice/model/format/text hash로 cache하며 API key는 backend header에만 들어간다. ElevenLabs가
실패하거나 설정되지 않으면 `ttsMode=IOS_LOCAL`로 질문별 폴백한다.

HeAR model weight는 일반 Apache-2.0 artifact가 아니라 HAI-DEF 이용약관이 적용된다. 팀 책임자가
약관을 수락하고 배포 조건을 검토하기 전에는 weight/TFLite 변환물을 public repository에
commit하지 않는다. inference code는 Apache-2.0이지만 model weight의 배포 조건은 별도다.

`JWT_SECRET`은 클라이언트용 값이 아니다. Swift/iOS·프론트엔드 팀원에게 전달하지 않는다.
하나의 공용 백엔드만 사용하면 그 배포 환경에만 보관한다. 팀원이 각자 독립 로컬 백엔드를
실행하면 각자 다른 secret을 써도 된다. 같은 도메인에서 여러 backend replica가 동일 로그인
토큰을 검증해야 할 때만 모든 replica에 같은 secret을 secret manager로 배포한다. 이 값을
변경하면 이전에 발급한 access/refresh token은 모두 무효화된다.

### 팀 공유 기준

`.env` 파일 전체나 비밀값을 Git·메신저로 전달하지 않는다. 팀원 역할별 전달 범위는 다음과
같다.

| 대상 | 전달할 값 | 전달하지 않을 값 |
|---|---|---|
| Swift/iOS 팀 | 공용 Backend base URL. `livekitUrl`과 room `accessToken`은 통화 API 응답으로 받음 | Deepgram/Gemini key, `JWT_SECRET`, `LIVEKIT_API_SECRET`, MinIO secret, APNs `.p8` |
| 공용 Backend 운영자 | 모든 서버 secret을 배포 환경의 secret manager에 설정 | Swift 소스·Git·팀 포털에 raw secret 노출 금지 |
| 독립 로컬 Backend 실행자 | 본인 Deepgram/Gemini key 또는 안전하게 전달받은 개발용 key, 본인이 생성한 `JWT_SECRET`; LiveKit/MinIO 값은 자신의 서버 설정과 일치시킴 | 다른 개발자의 개인 `.env` 전체를 복사할 필요 없음 |
| APNs 담당자 | 비밀이 아닌 Team ID, Key ID, Bundle ID, 환경을 서버 담당자와 공유; `.p8`은 서버 운영자에게만 보안 채널로 전달 | `.p8`을 Git·일반 메신저·팀 전체에 배포 금지 |

LAN 데모에서 팀에 공개해도 되는 값은 `PUBLIC_BASE_URL`, `LIVEKIT_URL`,
`S3_PUBLIC_ENDPOINT_URL` 같은 접속 주소다. 다만 iOS 앱은 실제로 공용 Backend base URL만
고정하면 되고, LiveKit URL과 토큰, PCM/TTS 만료 URL은 Backend 응답으로 받는다. ElevenLabs
API key와 voice ID는 backend `.env`에만 둔다.

#### pull 후 팀원 PC에서 전체 스택을 실행하는 최소 `.env`

팀원이 `backend/`에서 `docker compose up --build`를 실행해 실제 STT/LLM까지 재현하려면
저장소에 포함되지 않은 아래 값을 전달해야 한다.

```dotenv
DEEPGRAM_API_KEY=<팀 개발용 실제 값>
GEMINI_API_KEY=<팀 개발용 실제 값>
JWT_SECRET=<팀 공용 값 또는 각자 생성한 32자 이상 값>
MOCK_EXTERNAL_SERVICES=false
GEMINI_MODEL=gemini-3.6-flash
DEEPGRAM_MODEL=nova-3

# ElevenLabs server voice를 쓸 때만 추가. 미설정이면 iOS ko-KR local voice
QUESTION_TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=<팀 개발용 실제 값>
ELEVENLABS_VOICE_ID=<선택한 voice id>

# Mac 자체에서만 호출하면 localhost, 실제 iPhone이면 각 팀원 Mac의 LAN IP로 변경
PUBLIC_BASE_URL=http://<HOST>:8080
LIVEKIT_URL=ws://<HOST>:7880
S3_PUBLIC_ENDPOINT_URL=http://<HOST>:9000
```

팀원에게 가장 단순하게 전달하는 방법은 위 내용이 들어간 `backend/.env`를 별도로 전달하고,
각 팀원이 `<HOST>` 세 곳만 자기 환경에 맞게 바꾸는 것이다. Mac/Simulator만 사용하면
`<HOST>`는 `localhost`, 같은 Wi-Fi의 실제 iPhone이면 해당 Mac의 LAN IP다. 독립 로컬 DB를
쓰므로 `JWT_SECRET`은 같아도 되고 달라도 실행에는 문제가 없다.

이 저장소는 public이므로 실제 key가 든 `.env`를 커밋해서 `pull`만으로 provider 호출까지
동작하게 만들면 안 된다. 실제 API를 쓰는 로컬 실행은 `.env`를 저장소 밖에서 한 번 별도
전달하거나, 모든 팀원이 하나의 공용 Backend를 사용해야 한다. GitHub Actions secret은 CI에는
주입할 수 있지만 팀원의 로컬 `git pull`에는 전달되지 않는다. key를 실수로 public commit에
넣었다면 파일을 나중에 삭제하는 것만으로는 부족하며 해당 key를 즉시 폐기·재발급한다.

현재 Docker Compose와 `deploy/` 설정에는 서로 일치하는 개발용 LiveKit key/secret, MinIO
access key/secret, PostgreSQL 계정, Redis 설정이 이미 포함되어 있다. 따라서 저장소 그대로
실행하는 팀원에게 이 값들을 `.env`로 또 전달할 필요는 없다. 개발용 기본값을 변경할 때에만
`docker-compose.yml`, `deploy/livekit.yaml`, `deploy/egress.yaml`의 값을 모두 함께 맞춘다.

APNs 없이도 foreground 통화와 STT/LLM/음향 파이프라인은 실행된다. 실제 iPhone이 앱 종료·
백그라운드 상태에서도 전화를 받게 하려면 그때 추가로 `APNS_VOIP_ENABLED=true`, Team ID,
Key ID, Bundle ID, environment, `.p8` 파일 경로를 설정하고 실제 `.p8` 파일도 전달해야 한다.

APNs 작업은 단순 Apple ID 보유자가 아니라 Apple Developer Program 팀의 Account Holder 또는
Admin에게 요청한다. 요청 범위는 다음과 같다.

1. 콜록의 explicit Bundle ID를 확정하고 해당 App ID에 Push Notifications capability를 켠다.
2. 기존 APNs-enabled signing key를 안전하게 재사용하거나 새 key를 생성한다. Configure Key의
   Environment와 Key Restriction은 저장 후 변경할 수 없다. Bundle ID가 확정되기 전이라면
   Topic Scoped 대신 Team Scoped를 쓴다. APNs key는 팀당 개수 제한이 있어 잘못 묶인 key를
   버리는 비용이 크다. Environment는 TestFlight 전환까지 고려해 Sandbox & Production을 쓴다.
3. 서버 담당자에게 Team ID, Key ID, Bundle ID, sandbox/production 환경을 알려준다.
4. `.p8` 파일은 한 번만 다운로드할 수 있으므로 Git·메신저에 올리지 않고 secret manager 또는
   안전한 오프라인 경로로 서버에 전달한다.
5. Swift target도 같은 Team/Bundle ID로 서명하고 Xcode에서 Push Notifications와 필요한
   background mode를 활성화한다.

VoIP Services Certificate(`.p12`)는 만들지 않는다. 토큰 방식 `.p8` 하나로 sandbox와
production을 모두 처리하며 provider 코드도 `.p8` ES256 JWT만 사용한다. 값을 받으면 Swift 앱
없이도 `uv run python -m scripts.check_apns`로 자격증명을 먼저 검증한다. 더미 토큰에 대한
`BadDeviceToken` 응답이 성공 신호이고, `InvalidProviderToken`은 `.p8`/Key ID/Team ID 조합,
`BadTopic`·`TopicDisallowed`는 Bundle ID 또는 App ID capability 문제다. Xcode 직접 설치
빌드의 토큰은 sandbox에서만, TestFlight/App Store 빌드의 토큰은 production에서만 유효하다.

## 8. 다음 작업 우선순위

구현 순서와 phase별 완료 조건은 `backend/docs/implementation-plan-v2.md`를 따른다.

1. Phase 0: 미검증 cough 노출 차단, subject 계약 fixture, migration 기반 준비.
   schema guard는 2026-08-14 완료. 남은 것은 Alembic 도입(`alembic.ini`, `migrations/env.py`,
   baseline, Dockerfile COPY)과 compose Postgres 실측이며 설계는
   `backend/docs/schema-management-design.md` 4~5절에 있다. 2026-08-18 가비아 서버가
   `SCHEMA_AUTO_RESET=false`로 뜨므로 그 전에 끝나야 한다.
2. Phase 1: 관계 기반 가족/subject와 질환·복용약·걱정 프로필/동의 철회
3. Phase 2: `CallParticipant` 기반 양 참여자 분석·양방향 통화와 기존 iOS adapter
4. Phase 3: anchor+dynamic 질문 정책과 ElevenLabs
5. Phase 4: Q/A evidence 추출, 전체 transcript purge
6. Phase 5: observation 기반 통화 결과·주간·월간 리포트/공유
7. Phase 6: 실제 label/기기 검증을 통과한 음향값만 보조 연결
8. Phase 7: 피그마 iOS 흐름과 두 iPhone 전체 E2E

### 마감 일정과 남은 작업

| 날짜 | 일 |
|---|---|
| 2026-08-18(화) | 가비아 클라우드 개시. 2 vCore / 4 GB / 공인 IP 1개 |
| **2026-08-20(목)** | **제출 마감** |
| 2026-08-25(화) | 데모 |
| 2026-08-28(금) | 가비아 클라우드 종료 |

제출이 08-20이므로 실질 개발 기간은 그때까지다. 배포 제약은
`backend/docs/schema-management-design.md` 5절에 있다.

**제출(08-20) 전에 필요한 것**

| 항목 | 상태 | 근거 |
|---|---|---|
| schema guard | 완료 (2026-08-14) | 서버는 `SCHEMA_AUTO_RESET=false`로 뜬다 |
| **8주 더미 seed** | 완료 (2026-08-20 PostgreSQL 실측) | `scripts/seed_narrative_history.py` + `scripts/data/narrative_history.json`. 8개 ISO 주, ANCHOR/ROLLING 분리, W-0 포함, 재실행 가능. 데모 당일 아침 재실행 필요. 데모 통화는 KST 12시 이후에 해야 `AFTERNOON_EVENING` slot과 맞는다 |
| **두 iPhone 실기기 통화** | 미완 | 2026-08-13에 한 대만 검증했고 자녀 쪽은 API로 대신했다. **핵심 시연 자체이며 가장 큰 리스크다** |
| ElevenLabs key/voice ID | 미설정 | 없으면 iOS 로컬 TTS로 폴백돼 서버 TTS 차별점이 안 보인다 |
| 도메인·TLS 방식 | **미결정** | 공인 IP만으로는 Let's Encrypt 발급이 안 된다. 무료 도메인+LE / ATS 예외 확대 / 자체 서명 중 선택이 필요하다 |

**제출 이후로 미루는 것**

Phase 1~7은 피그마 계약에 맞추는 일반화 작업이며 새 시연 화면을 만들지 않는다. 남은 기간에
착수하면 위 항목을 완성할 시간을 잠식한다. Alembic도 서버 수명이 짧고 데모 데이터가 seed로
재생성되므로 제출 전 필수가 아니다.

기침은 보조 지표이므로 Phase 6의 모델 검증이 실패해도 값을 숨기면 핵심 demo를 막지 않는다.
외부 자격증명이나 법무 승인처럼 코드로 결정할 수 없는 조건만 해당 phase에서 사용자에게 묻는다.

다음 AI는 구현 전에 반드시 이 문서와 `backend/README.md`, 관련 service/test를 먼저 읽는다.
API를 바꿀 때에는 기존 camelCase 계약과 테스트를 유지하고, 판단 불가능한 건강 지표를 임의의
숫자로 채우지 않는다.

## 9. 변경 이력

- 2026-08-11: FastAPI/LiveKit/Deepgram/Gemini/기준선·리포트 전체 P0 골격 구현.
- 2026-08-11: LiveKit Cloud 대신 Server/Egress/Redis/MinIO self-hosted stack 확정.
- 2026-08-11: 클라이언트를 Swift 네이티브 iOS 우선으로 확정.
- 2026-08-11: APNs VoIP push provider와 PushKit/CallKit/LiveKit 통화 계약 추가.
- 2026-08-11: 공식 저장소를 `Frekion2002/14th-hackathon`으로 전환하고 HANDOFF 관리 시작.
- 2026-08-11: 외부 발급 키와 self-hosted 내부 secret을 구분한 자격증명 표 추가.
- 2026-08-11: 팀원·AI의 pull/read/verify/update/commit/push HANDOFF 운영 규칙 확정.
- 2026-08-11: Apple Developer Account Holder/Admin에게 요청할 APNs 준비 절차 추가.
- 2026-08-11: AI-1/AI-2/백엔드 완료 여부를 재감사하고 되묻기·음향·주 단위 signal 누락을 명시.
- 2026-08-11: Colima에서 self-hosted 7-service stack을 실제 기동하고 Deepgram/Gemini 실호출 검증.
- 2026-08-11: Gemini 500-token JSON truncation을 발견해 2,048 token 설정과 finishReason 검증 추가.
- 2026-08-11: Participant Egress+OGG 통합 오류를 발견해 audio Track Egress와
  `track_published` webhook orchestration으로 교체.
- 2026-08-11: 두 합성 참가자 통화의 LiveKit→MinIO→Deepgram→Gemini→purge 실제 E2E 성공.
- 2026-08-11: 매 턴 시작 전 `fetch`/원격 commit 비교를 강제하는 협업 규칙 추가.
- 2026-08-11: `/team` WebView 대응 팀 포털과 비밀값 없는 `/team/status.json` 추가.
- 2026-08-11: LLM prompt/eval·되묻기 규칙 detector와 AI-2 4종 음향 지표 설계 확정.
- 2026-08-11: prompt v2 parent evidence/polarity/grounding validator와 40-case eval suite 구현.
- 2026-08-11: Gemini key를 query에서 header로 이동하고 exception redaction 추가.
- 2026-08-11: 되묻기 규칙 detector/event/API/report와 Deepgram word timing 보존 구현.
- 2026-08-11: PCM AI-2 4종 prototype, analyzer version 저장, calendar-week baseline 수정.
- 2026-08-11: Deepgram Aura 한국어 TTS 미지원을 확인하고 iOS local `ko-KR` TTS로 확정.
- 2026-08-13: APNs 자격증명 점검 CLI `scripts/check_apns.py`와 Apple Developer/Xcode PushKit 설정 절차 추가.
- 2026-08-13: 실제 Apple 자격증명으로 APNs sandbox 자격증명 점검 통과. 발급한 key는 sandbox 전용.
- 2026-08-13: iOS Xcode project를 `ios/`로 추가하고 PushKit→CallKit 실기기 수신을 검증.
- 2026-08-13: iOS 로그인·발신·수신 통화 화면과 LiveKit 연동, 질문 로컬 TTS 구현.
- 2026-08-13: Docker 없이 실행하는 네이티브 LiveKit/MinIO 구성과 가족 seed 스크립트 추가.
- 2026-08-13: 실기기 PCM 업로드로 Egress 없이 STT/LLM/음향 파이프라인 완주. 품질 게이트를 실측으로 -55 dBFS/p90 보정.
- 2026-08-13: 기침 detector 재현율 0을 합성 검증으로 확인하고 보정 계획을 calibration-todo.md에 정리.
- 2026-08-13: `feat/ios-pushkit` 4개 커밋을 main에 통합하고 PCM buffer 수명, 통화 화면 상태,
  Docker analyzer 설정, raw-only replay 회귀와 iOS 17 deployment target을 보완.
- 2026-08-13: ElevenLabs 한국어 질문 TTS를 backend 생성·cache·만료 URL로 추가하고 iOS local
  폴백을 유지. voice/preview CLI와 provider 장애 test 추가.
- 2026-08-13: iPhone 2대 통화 절차와 DB 기반 Egress→양 화자 STT→AI-2→purge 자동 판정 CLI 추가.
- 2026-08-13: 두 iPhone preflight CLI, Docker APNs `.p8` read-only mount와 개발용 LAN ATS 설명 추가.
- 2026-08-13: 통화 화면에 ElevenLabs source badge와 player 상태/폴백/상대 연결 중단 로그를 추가해
  두 iPhone 현장에서 server TTS 성공 여부를 명시적으로 증빙하도록 보강.
- 2026-08-13: HeAR 논문·PyTorch model card·공개 MobileNetV3 event detector와 Hyfe/ResAppDx/
  Swaasa/Sonde/Winterlight/hearWHO를 조사. cough count는 HeAR Small/YAMNet bake-off로 결정하고,
  되묻기는 난청 판정이 아닌 문맥적 대화 수리 관찰값으로 제한하기로 정리.
- 2026-08-13: PCM은 비압축 sample 형식이지만 iOS voice processing을 이미 거칠 수 있음을 명시.
- 2026-08-13: 새 피그마 흐름을 기준으로 제품 중심축을 본인 확인 건강 프로필→맞춤 질문→통화
  자기보고→주·월간 리포트로 확정. 부모/자녀 양방향 subject 모델, 복용약·걱정·입력 출처·본인
  확인, Q/A 근거 추출, 리포트 출처 분리를 설계하고 현재 코드와의 차이를 기록.
- 2026-08-13: 사용자가 결정 불가능 항목 외 권장 기본안을 승인. 관계 기반 role, callee subject,
  최소 전사 보관, template 질문, observation report 등 기본안과 Phase 0~7 실행 계획을 확정.
- 2026-08-13: 한 통화의 caller/callee를 모두 각자의 건강 subject로 동시 분석하도록 변경.
  질문 target은 callee로 유지하고, 필수 동의를 온보딩 진입 조건으로 삼아 정상 통화는 양쪽
  분석을 항상 활성화하기로 확정.
- 2026-08-13: cough rate를 통화 표본 내 환산값으로 제한하고, 표준 질문 기반 대화 변화와 선택적 HealthKit
  활동 추세를 결합하는 후속 제품 방향을 `voice-health-model-research.md`에 추가.
- 2026-08-13: 초기 서비스 기획안을 개조식으로 재구성. 중복을 제거하고 양 참여자 분석,
  필수 온보딩 동의, Deepgram/Gemini 역할, 근거 기반 리포트, 미검증 음향값 비노출을 반영.
- 2026-08-13: Egress worker가 없는 raw-only 개발 구성에서 `/accept`가 Track Egress 응답을
  기다리며 통화 연결과 DB transaction을 지연시키던 문제를 수정. raw-only일 때 Egress 조회·시작을
  모두 생략하고, 일반 Compose의 양쪽 Track Egress 경로는 유지.
- 2026-08-13: 공개 도메인 라벨 음원 8개로 `transient-heuristic-v1` 실패를 재현. 기침 4개 전부
  0회, 최고 점수 0.609로 임계값 0.65 미달이며 0.40으로 낮추면 웃음이 7회로 1위다. 항별 분해
  결과 죽은 항은 energy 하나이고(clip 자신의 median을 기준선으로 삼는 자기참조 구조),
  crest는 통화 조건에서만 붕괴한다는 점을 calibration-todo.md 1절에 정정 기록.
- 2026-08-13: `COUGH_EVENTS`를 `UNMEASURABLE(DETECTOR_NOT_VALIDATED)`로 고정하고 기침 detector를
  HeAR event detector ONNX 변환본으로 교체 (analyzer v5). 단위가 `회`에서 `구간`으로 바뀌었고
  `onnxruntime`만 추가해 TensorFlow 없이 돈다. 모델은 HAI-DEF 약관 대상이라 저장소에 넣지 않고
  `scripts/fetch_cough_model.py`로 revision 고정·sha256 검증·NOTICE 생성을 거쳐 각자 받는다.
  `cough_detector_validated` 기본값은 `False`이므로 사용자에게 보이는 동작은 아직 변화 없다.
- 2026-08-14: 기동 시 모델과 DB 스키마를 비교하는 schema guard 추가. `create_all()`이 기존
  테이블을 ALTER하지 않아 스키마 변경이 조용히 무시되던 문제를 막는다. 신규 테이블만 늘어난
  경우(`ADDITIVE`)는 데이터를 지우지 않고 그 테이블만 만들고, 기존 테이블이 어긋나면
  (`DRIFTED`) 로컬은 재생성한다. `SCHEMA_AUTO_RESET=false`인 배포 환경에서는 스키마를 전혀
  수정하지 않고 기동을 거부한다. 설계는 `backend/docs/schema-management-design.md`.
- 2026-08-15: 피그마 `일하자` 페이지(88:516)를 기준으로 iOS 홈 대시보드와 하단 5탭 셸을 구현.
  Foundation/Typography 토큰을 `ColloTheme.swift`로 분리하고, 홈 카드·리포트 탭 문구를
  `/parents/{id}/reports`, `/daily-questions`, `/calls`의 실제 응답에만 바인딩했다. 리포트가
  `READY`가 아니면 변화 문장을 만들지 않고 `기준선 수집 중`을 표시한다. 피그마 카피의
  `인지 장애 우려` 같은 위험군 라벨과 정적 추이 SVG는 그대로 옮기지 않고, 각각 통화
  자기보고 항목과 서버 `acousticTrends` 기반 그래프로 대체했다(7절 금지선 유지).
  `CollogAPI`에 query string 전송 경로를 추가했다. 기존 `appending(path:)`는 `?`를 `%3F`로
  이스케이프해 query가 붙은 endpoint를 호출할 수 없었다.
- 2026-08-14: 가비아 클라우드 지원 기간(08-18~08-28)과 사양(2 vCore / 4 GB / 공인 IP 1개)을
  기준으로 배포 제약을 정리. 공인 IP만으로는 Let's Encrypt 발급이 불가능해 도메인·TLS 방식
  결정이 남았다. `scripts/seed_demo_family.py`가 통화·기준선·리포트를 만들지 않아 과거 더미
  seed가 없다는 점도 함께 확인했다.
- 2026-08-15: 위 항목의 "4 GB에 LiveKit Egress를 올릴 수 없다"를 정정. 근거 두 개가 모두
  틀렸다. `shm_size`는 선점이 아니라 `/dev/shm` tmpfs 상한이고, 이 프로젝트는
  `start_track_egress`를 쓰므로 Chrome이 뜨는 Room Composite 경로가 아니다. 스택 합계는
  대략 1.3 GB로 추정되어 양쪽 Track Egress를 켠 정상 구성으로 배포한다.
  `ALLOW_RAW_ONLY_ANALYSIS`는 기본값 `false`로 두고 08-18 실측이 실패할 때만 켠다. 이에 따라
  Phase 2의 양쪽 기기 PCM upload는 배포 선행 조건이 아니다.
- 2026-08-15: 제출 마감(08-20)과 데모(08-25) 일정을 반영해 8절을 다시 정리. 시연 더미
  데이터에 필요한 분량이 4주가 아니라 8주임을 확인하고 근거를 `의도적으로 미완료`에 기록.
- 2026-08-15: 클라우드 배포 설정과 사전 검증 절차 추가. `deploy/livekit.yaml`의
  `rtc.use_external_ip: false`는 로컬 LAN 전용이며 공인 IP 뒤에서는 LiveKit이 사설 IP를 ICE
  후보로 광고해 미디어가 연결되지 않는다. signaling은 정상이라 통화가 연결된 것처럼 보이고
  소리만 나지 않는다. `deploy/livekit-cloud.yaml`을 분리하고 compose가
  `LIVEKIT_CONFIG_FILE`로 고르게 했다. 절차는 `backend/docs/cloud-deploy.md`. 2026-08-18
  가비아 배포 전에 같은 사양(2 vCPU / 4 GB)의 EC2에서 한 번 밟아 메모리 추정과 LiveKit NAT
  통과를 실측한다.
- 2026-08-17: 8주 시연용 더미 seed를 구현했다. `services/demo_seed.py`가 서로 다른 ISO 주
  8개를 만들어 ANCHOR(가장 이른 4주)와 ROLLING(최근 4주)을 분리하고 이번 주(W-0)를 포함한다.
  API로는 만들 수 없어(`started_at`/`observed_at`이 `utcnow()` 고정) DB에 직접 삽입하지만
  `Baseline`/`ChangeSignal`은 직접 만들지 않고 `SignalService.process_call()`을 호출해 실제
  서비스 코드가 만들게 한다. 검증되지 않은 기침·휴지·F0는 생성하지 않고 발화 속도만 넣는다.
  실행 CLI는 `scripts/seed_demo_history.py`이며 `demo-history-*` room만 교체한다.
- 2026-08-17: 리포트 상태 판정을 "COLLECTING 기준선이 하나라도 있으면 전체 미준비"에서
  "READY 기준선이 하나라도 있으면 READY"로 바꿨다. 음향 4종 중 발화 속도만 값이 나오는 현재
  상태에서 이전 규칙은 정상 데이터에서도 리포트를 영구히 `기준선 수집 중`으로 묶었다.
  함께 `recentAcousticHistory`(최근 5주)와 `containsDemoData`/`demoDataNotice`를 추가했다.
- 2026-08-17: 리포트 저장 실패 잠재 버그를 수정했다. 변화 signal의 `observedAt`이 datetime
  이라 `Report.snapshot` JSON 직렬화에서 깨질 수 있었다. `jsonable_encoder`를 snapshot 전체에
  적용했다. 이전에는 변화 signal이 실제로 생기는 경우가 없어 드러나지 않았고, 8주 seed가
  signal을 만들면서 재현됐다.
- 2026-08-17: 시스템 구성도(`backend/docs/system-architecture.html`)와 운영비 산정
  (`backend/docs/operating-cost.html`)을 추가했다. 구성도는 Caddy/TLS가 아직 없다는 점,
  MinIO 9000이 iOS에 직접 노출된다는 점, UDP 7882는 프록시 대상이 아니라는 점을 현재 구현과
  권장 구성으로 나눠 적었다. 운영비는 100가구 월 약 $107(예비비 20% 포함), 가구당 약 $1.07로
  산정했고 변동비의 94%가 Deepgram STT다. 비용을 줄이려면 모델 선택이 아니라 전사 분량을
  건드려야 한다는 결론이다.
- 2026-08-17: 팀 공유용 운영비 시각화 `output/html/collog-operating-cost.html`를 추가했다.
  `backend/docs/operating-cost.html`과 같은 숫자를 쓰지만 용도가 다르다. 산정 문서는 계산식과
  출처가 정본이고, 이 파일은 결론을 먼저 보여주는 발표·검토용이다. 외부 CDN 없이 inline SVG로
  그려 오프라인에서 열린다. 원화는 2026-08-14 종가 1 USD = 1,417.53원으로 병기했다.
  절감 수단 중 "무음 제거"는 절감 폭을 측정하지 않았으므로 차트에 숫자를 넣지 않고 별도
  경고로만 적었다.
- 2026-08-17: Deepgram 과금 구조를 공식 FAQ로 확인해 운영비 문서에 절을 추가했다. 구독이
  아니라 초 단위 pay-as-you-go 크레딧이고 반올림이 없다("true per-second billing"). 따라서
  무음 제거 절감이 선형으로 반영되며 짧은 통화가 불리하지 않다. 신규 계정 $200 무료 크레딧은
  만료가 없어 우리 요율로 41,667분, 월 9,600분 기준 약 4.3개월치다. 반대로 절감이 안 되는
  것도 확인했다. 2트랙을 stereo 한 파일로 합쳐도 "billed for 20 minutes"라 절감이 0이고,
  구형 모델(Nova-2 streaming $0.35/hr, Base $0.87/hr, Enhanced $0.99/hr)은 우리가 쓰는
  Nova-3 pre-recorded $0.288/hr보다 비싸다. Growth는 연 $4,000+ 약정에 초과분 +10%
  프리미엄이라 연 사용량 $553인 우리 규모에는 손해다.
- 2026-08-17: 위 확인 과정에서 `$0.0048/분`이 Model Improvement Program opt-in 요율임을
  발견했다. `services/deepgram.py`가 `mip_opt_out`을 보내지 않아 실제 건강정보를 보내는 순간
  학습 데이터 이용에 동의한 상태가 된다. opt-out 요율이 공개되지 않아 비용 영향을 계산할 수
  없으므로 코드는 바꾸지 않고 7절 불변조건과 3절 미완료에 결정 사항으로 기록했다.
- 2026-08-17: 운영비 문서 두 개의 역할을 분리하고 시각화 문서를 다시 썼다. 이전 버전은
  "월 총액 → 구성비 → 감도 → 절감"을 다 담아 정작 자주 묻는 것(어느 provider가 규모별로
  얼마인가)이 표 하나에 안 보였다. `output/html/`은 **규모별 provider 비용표를 첫 화면에**
  두고 무료 크레딧, 계정 재발급 여부, 무효 조건만 남긴 6개 절로 줄였다. 감도 분석·전송량
  점검·절감 수단은 `backend/docs/operating-cost.html`에만 둔다.
- 2026-08-17: 두 문서에 규모별 provider 내역(10 / 100 / 1,000 / 10,000가구)과 무료 크레딧
  소진 기간을 추가했다. Deepgram 비중은 100가구 84%, 1,000가구 이상 94%다. $200 크레딧은
  우리 요율로 694시간이고 100가구 기준 4.3개월, 1,000가구면 13일이면 끝난다. 100가구 첫 달
  실지출은 $32.70이다.
- 2026-08-17: 무료 크레딧을 계정마다 다시 받는 방식이 성립하지 않음을 약관으로 확인해
  기록했다. 무료 크레딧은 조직 내 계정 간에도 이전 불가이고, ToS 5-6절이 프로모션 자격 판정에
  device ID·결제수단·이메일을 쓸 수 있다고 명시하며, 2-4-(13)절이 사용 제한 우회를 금지한다.
  백엔드가 API 키 하나로 도는 구조라 기술적으로도 합칠 수 없다.
- 2026-08-17: ToS 3-5-(4)절을 확인해 **BAA 없이 HIPAA상 PHI를 Deepgram에 보내면 약관 위반**
  임을 문서에 남겼다. BAA는 Enterprise 고객에게만 제공된다. 한국 서비스에 HIPAA가 직접
  적용되는지는 법무 판단이 필요하지만, 미국 사용자를 받으면 공개 단가가 아니라 Enterprise
  협상가가 되므로 운영비 모델 자체가 달라진다.
- 2026-08-18: 8주 데모 seed의 주별 내용을 코드에서 분리해 `app/data/demo_history.json`으로
  옮겼다. `demo_seed.py`의 `DEFAULT_SPEECH_RATE_SERIES`와 `_extraction_for_week()`를 제거하고
  `load_spec()`이 JSON을 읽어 검증한다. 검증 항목은 time slot 유효값, 질문 pool에 있는
  questionId, `weekOffset` 중복·미래 주 금지, 이번 주(0) 포함, 값이 전부 같은지(MAD=0),
  `selfReport` 4개 필드 존재다. 잘못된 spec은 조용히 통과하지 않고 실행을 거부한다.
  틀린 spec은 리포트가 정상처럼 보이면서 기준선이 없는 상태를 만들기 때문에 hard fail로 했다.
  `scripts/seed_demo_history.py`에 `--spec` 옵션을 추가했다.
- 2026-08-18: seed 내용을 데모 시나리오에 맞춰 교체했다. time slot을 `MORNING`에서
  `AFTERNOON_EVENING`으로, 발화 속도를 초기 4주 268/262/271/258, 최근 4주 245/238/229/221로
  바꿨다. 실측 결과 ANCHOR median 265.0(MAD 4.5), ROLLING median 233.5(MAD 8.0)로 둘 다
  `READY`이고, W-3부터 4주 연속 유의한 하락이 잡혀 W-0에서 승격 signal
  `발화 속도 4주 연속 변화, 처음 대비 -17%`가 나온다. 자기보고는 "말했다/언급했다/답했다"로만
  적어 원인 추정과 진단·위험도 표현을 넣지 않았다. `askedQuestionIds`는 기존 질문 pool의 ID만
  쓴다.
- 2026-08-18: 위 검증 중 `acuteSignals`가 항상 0건인 이유를 확인해 3절에 기록했다.
  `process_call()`이 현재 통화를 제외하는데 ROLLING 창 4주와 필요 표본 4가 같아서, 주 1회
  통화로는 `vs_rolling`이 계산될 수 없다. 지표 정의 변경이 필요한 사안이라 코드는 바꾸지 않았다.
- 2026-08-18: 8주 seed를 Compose PostgreSQL 17에서 실제로 실행해 6절에 실측치를 남겼다.
  `seed_demo_family` → `seed_demo_history` → 리포트 API를 실제 HTTP로 완주했고, 기준선
  (ANCHOR 265.0/MAD 4.5, ROLLING 233.5/MAD 8.0)과 승격 signal, 멱등성, production 가드까지
  격리 SQLite와 같은 결과를 확인했다. 이로써 `의도적으로 미완료`의 "Compose PostgreSQL 실행
  기록 없음" 항목을 닫았다. 이 과정에서 **backend image를 재빌드하지 않으면 컨테이너가 옛
  코드로 돈다**는 점을 확인해 6절에 명시했다. 남은 것은 가비아 서버 실측이다.
- 2026-08-20: 저장소를 `Frekion2002/14th-hackathon`에서 조직 `Collog-App/14th-hackathon`으로
  전송했다. 브랜치 8개와 PR #1은 그대로 넘어왔고 구 URL은 GitHub이 리다이렉트한다. 코드·문서의
  `TEAM_PORTAL_REPOSITORY_URL` 기본값(`.env.example`, `docker-compose.yml`, `config.py`),
  `cloud-deploy.md`의 clone 주소, 구성도 HTML의 저장소 표기를 새 주소로 바꿨다. 팀원은 각자
  `git remote set-url origin https://github.com/Collog-App/14th-hackathon.git`을 실행한다.
- 2026-08-20: `fix/pipeline-report-and-auth-gates`를 main에 통합하면서 **중복이던 seed 구현을
  삭제했다.** 같은 8주 시연 데이터를 두 사람이 각각 만들어 두 벌이 됐고, 팀원 쪽을 남겼다.
  삭제한 것은 `app/services/demo_seed.py`, `app/data/demo_history.json`,
  `scripts/seed_demo_history.py`, `tests/test_demo_history.py`다. 남은 것은
  `scripts/seed_narrative_history.py` + `scripts/data/narrative_history.json`이며 room 접두사는
  `demo-history-`가 아니라 `narrative-`다. 문서의 실행 절차도 그쪽으로 바꿨다.
- 2026-08-20: 위 통합에서 `reports.py`가 유일한 충돌이었고 브랜치 쪽을 채택했다. 브랜치 구현이
  더 정확하다. 기간 경계를 KST로 계산해 한국 시간 자정~오전 9시에 이번 주 통화가 빠지는 버그를
  고쳤고, 상태 판정에서 **채워질 수 없는 기준선**(통화 없는 시간대, 미검증 기침 detector)을
  제외해 리포트가 영구히 `BASELINE_COLLECTING`에 묶이지 않게 했다. 이 과정에서 2026-08-17에
  넣은 `recentAcousticHistory`와 `containsDemoData`/`demoDataNotice`가 함께 사라졌다.
  소비하는 클라이언트 코드가 없어 되살리지 않았고, 더미 표시 필요성은 3절 미완료로 옮겼다.
- 2026-08-20: 2026-08-17의 "리포트 저장 실패 잠재 버그" 주장을 **정정한다.** `jsonable_encoder`
  없이도 `observedAt` datetime이 든 snapshot이 PostgreSQL에 정상 저장되고 리포트 API가 200을
  반환하는 것을 실측했다. 그 수정은 필요하지 않았다.
- 2026-08-20: 통합 직후 main이 lint 실패 상태였던 것을 고쳤다. `pipeline.py`의 로그 한 줄이
  E501, `seed_narrative_history.py`가 async 안에서 `pathlib`을 써 ASYNC240이었다. 파일 읽기를
  동기 함수로 분리했다. 브랜치를 머지할 때 `ruff`를 다시 돌려야 한다.
- 2026-10-24: Android 포팅을 위해 Google 로그인 엔드포인트, `google_subject` 컬럼과
  `google_login_challenges` 테이블(migration 0011), 검증기·라우트 테스트 40개를 추가했다.
- 2026-10-24: Android 기기로 수신 통화·리포트 알림을 FCM HTTP v1으로 보내는 발송기와 플랫폼별 발송
  분기, 테스트 12개를 추가했다.
- 2026-10-24: FCM 보안 리뷰 반영. 빈 키 경로를 미설정으로 처리(기동 실패 방지), 리포트 재시도 때
  이미 받은 기기 재발송 방지, 수신 기기 사전 검사에 ANDROID 포함, 죽은 토큰 판정을 `message.token`
  지목 시로 한정, 토큰 정리를 해당 기기 행으로 한정, token_uri 고정, 서비스 계정 JSON gitignore.
