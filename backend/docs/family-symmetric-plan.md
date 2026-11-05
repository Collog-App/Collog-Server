# 가족 대칭 구조(B안) 이행 계획

마지막 갱신: 2026-11-04(S3) · 브랜치 `feat/family-symmetric`(`feat/android-port` 위)

## 왜

피그마 「최종본」(노드 269:3887)과 `profile-question-report-design.md`(08-13)는 부모·자녀 구분 없이
**가족 누구나 자기 건강 기록을 갖고, 서로 전화하고, 통화한 두 사람을 각각 분석**하는 구조다.
지금 서버는 계정 역할(PARENT/CHILD)에 묶여 있다.

| 어디 | 지금 | 목표 |
|---|---|---|
| 초대 | 생성 = CHILD, 수락 = PARENT | 가족 누구나 생성, 가족이 없는 누구나 수락 |
| 통화 | 부모-자녀 짝끼리만(양방향은 됨) | 같은 가족 구성원 누구나 |
| 분석·리포트 | 부모만 | 통화한 두 사람 모두(S3) |
| 질환 프로필·오늘의 질문 | 부모만 | 누구나 자기 것 |
| 가입 정보 | 이름만 | 닉네임·성별·생년월일 |

## 단계

### S1 가족 대칭 (작음)
- 한 사람은 한 가족에만 속한다(3대 가족도 한 묶음). 가족이 없으면 첫 초대 때 만든다.
- 초대 생성: 가족 구성원 누구나. 초대 수락: 가족이 없거나, 자기 혼자뿐인 가족에만 있는 사람 누구나
  (혼자뿐인 가족은 정리한다). 다른 사람이 있는 가족에 이미 속해 있으면 409.
- 구성원 목록: 만든 사람과 구성원을 같은 형식으로 준다. `canInvite` = 구성원 누구나.
  구성원마다 `acceptedAt`, 동의 상태를 준다.
- 통화: 같은 가족 구성원 둘이면 누구나. `calls.parent_id` 는 S3 전까지 **분석 대상**으로 쓴다 —
  두 사람 중 PARENT 역할이 정확히 하나면 그 사람(지금 iOS 동작 유지), 아니면 받는 사람.
  `child_id` 는 다른 한 사람. 컬럼 이름은 호환을 위해 그대로 둔다.
- 접근 권한: `/parents/{parentId}/...` 의 parentId 를 「대상 사용자」로 읽는다. 본인 또는 같은
  가족 구성원(S4 공유 범위 안)이면 허용. 역할 검사는 없앤다. 경로는 iOS 호환을 위해 유지.
- 역할 칸(`users.role`)은 응답 호환용으로만 남긴다. 새 안드로이드 가입은 역할을 보내지 않는다.

### S2 사용자 정보·본인 질환 (작음)
- `users` 에 `nickname`(유일, 2~10자 한글·영문·숫자), `gender`(`MALE`/`FEMALE`), `birth_date` 추가(기존
  사용자를 위해 nullable). `UserView` 에 같은 칸과 `signupCompleted`(닉네임 있음) 추가.
- `GET /v1/account/nickname-availability?nickname=` → `{available, reason}`.
- `PATCH /v1/account/profile` `{nickname, gender, birthDate}` → `UserView`. 닉네임 중복이면 409.
- 질환 프로필(`/parents/{id}/profile`)을 누구나 자기 것으로 쓴다. 같은 가족 구성원도 편집할 수 있다
  (설계 문서의 「가족 제안 → 본인 확인」은 S3 뒤로 미룬다). 저장 조건 = 대상 본인의 분석 동의.
- 이메일: Google·카카오 로그인에서 받은 값을 `users.email`(nullable)에 저장해 계정 정보에 보인다. 변경은 없다.

### S3 두 사람 분석 (2026-11-04 구현, 브랜치 `feat/two-participant-analysis`, 설정 기본 꺼짐)
- 분석 비용이 대략 두 배가 된다 → `operating-cost.html` 기준으로 다시 계산하고 팀 결정 뒤 켠다.
- 스위치: `TWO_PARTICIPANT_ANALYSIS_ENABLED`(기본 `false`). **통화를 만들 때만** 읽고 결정은 `calls.analysis_mode`
  (`SINGLE`/`BOTH`)에 남긴다. 꺼져 있으면 지금과 똑같이 `calls.parent_id` 한 사람만 분석하고, 켜기 전에 만든 통화는
  켠 뒤에 처리해도 `SINGLE` 이다. 마이그레이션 `0016_two_participant_analysis`.
- 데이터: `call_participants(call_id, user_id, role CALLER/CALLEE, speaker_label PARENT/CHILD, analysis_status
  PENDING/ANALYZED/EXCLUDED/FAILED, exclusion_reason, speech_sec)` — `BOTH` 통화만 행이 있다(옛 통화는 채우지 않음).
  `speaker_label` 은 전사 segment 의 speaker 값이고 `parent_id` 인 사람이 `PARENT` 다. `audio_assets.owner_user_id`(누구의
  목소리인지), 분석 표 다섯 개(`health_extractions`·`extraction_evidence`·`acoustic_analysis_runs`·`acoustic_features`·
  `repeat_events`)에 `subject_user_id`, `calls.question_target_id`. 옛 행은 `parent_id`(자녀 Egress 는 `child_id`)로 채웠다.
  통화당 한 행이던 세 표는 `(call_id, subject_user_id)` 유일로 바뀌었다.
- 통화 생성(`BOTH`): 참여자 두 줄, 연결 질문 = 받는 사람의 프로필(`question_target_id` = 받는 사람, 최근 질문 제외도 이
  칸 기준), `rawCaptureRequired` 는 녹음이 켜져 있으면 두 사람 모두 true. 원시 오디오 upload-url 은 두 참여자 모두 받고,
  대기 중 자산은 올린 사람별로 찾으며, complete 는 자기 자산(`owner_user_id`)만 된다.
- 파이프라인(`BOTH`): 공통 단계(상태·동의·보관 한도·자산 확인) 뒤에 나뉜다. 예전처럼 Egress·원시 오디오를 기다리고
  (`raw_audio_wait_seconds`), 자기 Egress 가 없는 참여자는 `FAILED`(`EGRESS_MISSING`). 선점은 지금의 조건부 UPDATE 한 번.
  STT 는 자기 Egress 가 있는 참여자의 트랙마다 끝나는 대로 참여자 행(`call_participants.stt_result`)에 잠깐 남기고, 그
  트랙이 다 모이면 transcript 로 합친 뒤 지운다(남은 참여자를 닫을 때도 지운다). 재시도 때 transcript 가 이미 있으면 그대로
  쓰고(Deepgram 호출 없음), 없으면 결과가 없는 트랙만 Deepgram 에 보낸다(`SINGLE` 은 지금처럼 다시 전사). 그 뒤 참여자마다: 발화가
  `parent_min_speech_seconds` 보다 짧으면 `EXCLUDED`(`INSUFFICIENT_SPEECH`), 그 사람의 추출이 아직 없을 때만 Gemini 를
  부르고 바로 커밋(재시도 때 재과금 없음), 음향은 자기 원시 오디오(없으면 자기 Egress, 이때 F0·기침은 지금처럼
  `SOURCE_NOT_RAW`), 되묻기·기준선도 그 사람 것으로 만들고 `ANALYZED`. 일시 오류는 다른 참여자를 마저 끝낸 뒤 기존 재시도로
  넘기고, 영구 오류는 그 참여자만 `FAILED`. 통화 상태 = 한 명이라도 `ANALYZED` 면 `ANALYZED`(리포트 알림 그대로), 아니면
  `FAILED` 가 있으면 `ANALYSIS_FAILED`, 아니면 `ANALYSIS_EXCLUDED`. 원본 오디오는 모든 참여자가 끝난 뒤에만 지운다. 재시도
  소진·24시간 보관 한도에서는 남은 참여자를 `FAILED` 로 닫고 지우며, 동의 철회는 기다리지 않고 바로 지운다. 전체 전사
  보관은 지금과 같다. raw-only 개발 모드(`ALLOW_RAW_ONLY_ANALYSIS`)는 `SINGLE` 만 된다. 거절·미응답·녹음 꺼짐으로
  분석 없이 끝난 통화는 참여자를 `EXCLUDED` 로 닫는다(사유 = 녹음이 꺼진 이유, 거절·미응답은 `CALL_NOT_ANSWERED`).
- 추출 프롬프트·검증기·mock 은 그대로 쓴다. 분석할 사람의 segment 를 `PARENT`, 상대를 `CHILD` 로 바꿔 넣고(segment id 는
  그대로라 근거가 저장된 전사를 가리킨다) 결과에 `subject_user_id` 를 단다. **프롬프트는 분석 대상을 여전히 「부모」라고
  부른다** — 자녀·형제 발화를 「부모」로 읽혀도 품질이 같은지 relabel 한 평가(`evals/`)를 먼저 돌린 뒤 설정을 켠다.
- 읽기(모드와 무관): 대상 X 의 통화 = `parent_id` 가 X 인 `SINGLE` 통화 + X 의 참여자 행이 끝난(`ANALYZED`/`EXCLUDED`)
  `BOTH` 통화. 리포트(추출·음향·신호·되묻기)·기준선·변화 신호가 대상별로 나뉜다. `/parents/{id}/calls` 는 지금처럼 상태와
  상관없이 `parent_id` 통화에 X 가 참여한 `BOTH` 통화를 더한다. 경로 `/v1/parents/{parentId}/...` 는 그대로.
- API: 통화 응답(`GET /calls/{id}`, `/end`, `/parents/{id}/calls`)은 `BOTH` 통화에만 `analysisMode` 와
  `participants[]`(`userId, role, speakerLabel, analysisStatus, exclusionReason, speechSec`)를 더한다.
  `/calls/{id}/transcript`·`/extraction`·`/acoustic-features` 는 `?subjectId=` 를 받는다(비우면 `parent_id`, 지금과 같음).
  통화 대상이 아니면 404, 다른 사람을 고르면 그 사람의 리포트 공유 범위(S4)를 본다. transcript 의 `excluded`·
  `exclusionReason`·`parentSpeechSec` 는 고른 대상 기준이다.
- 계정 삭제: 지금처럼 그 사람이 참여한 통화를 모두 지우고, `call_participants` 와 `BOTH` 통화 상대 쪽의 리포트·기준선도 지운다.
- **되돌리기 주의**: `BOTH` 행이 생긴 뒤 S3 이전 바이너리로 되돌리면 `call_id` 로만 찾는 옛 코드가 한 사람의 추출·음향을
  다른 사람 것처럼 보일 수 있다. 되돌리기 전에 `BOTH` 통화에서 나온 행(`analysis_mode='BOTH'` 통화의 분석 표 행·참여자 행,
  상대 쪽 리포트·기준선)을 먼저 지운다. 설정만 끄는 것은 안전하다(이미 만든 `BOTH` 통화는 `BOTH` 로 끝난다).

### S4 구성원 관리·공유 범위 (중간)
- `DELETE /v1/families/{familyId}/members/{memberId}`: 대기 중 초대는 구성원 누구나, 연결된 구성원은
  가족을 만든 사람 또는 본인(나가기).
- 공유 범위: 각자 자기 기록을 어느 구성원에게 보일지 정한다. `family_shares(owner, viewer, report, timeline)`,
  기본값 모두 허용. `GET/PUT /v1/account/sharing`. S1 접근 권한이 이것을 본다.
- 마케팅 수신 동의: `users.marketing_opt_in` + `PATCH /v1/account/preferences`.

### S5 카카오 로그인 (2026-11-04 구현, 브랜치 `feat/kakao-login`)
- Google 과 같은 방식: `POST /v1/auth/kakao/challenge` → `{challengeId, nonce, expiresIn}`, 앱이 카카오 SDK 로그인에
  nonce 를 넣어 받은 OpenID Connect ID 토큰을 `POST /v1/auth/kakao {idToken, challengeId, name?}` 로 보낸다.
- 서버 검증: iss `https://kauth.kakao.com`, RS256, 공개키 `https://kauth.kakao.com/.well-known/jwks.json`,
  aud = `KAKAO_APP_KEY`(네이티브 앱 키), nonce 일치, 준비 요청은 한 번만. 비우면 503.
- 계정은 `users.kakao_subject` 로 찾고 없으면 역할 없이(저장은 CHILD) 만든다 — 가족은 첫 초대 때. 이름 기본값은
  ID 토큰 `nickname`, 이메일은 `email`(이용자가 제공에 동의했을 때만 들어온다). 마이그레이션 `0015_kakao_login`.
- 카카오 콘솔: 카카오 로그인과 OpenID Connect 를 켜야 ID 토큰이 나온다. 계정 삭제 때 카카오 연결 끊기(unlink)는 하지 않는다(Google 도 같음).

### S6 PASS 본인인증 (대행사 계약 뒤)
- 계약 전까지 앱의 PASS 화면은 꺼 둔다.

## API 약속 (S1·S2·S4, 안드로이드 A3·A4 가 이것에 맞춘다)
JSON 은 기존처럼 camelCase, 경로 접두어 `/v1`.

- `UserView`(로그인·`/account/*` 응답의 사용자)에 추가: `nickname: str|null`, `gender: "MALE"|"FEMALE"|null`,
  `birthDate: "YYYY-MM-DD"|null`, `email: str|null`, `marketingOptIn: bool`, `signupCompleted: bool`(닉네임 있음).
- `GET /v1/account/nickname-availability?nickname=X` → `{"available": bool, "reason": "TAKEN"|"INVALID"|null}`.
- `PATCH /v1/account/profile` `{"nickname", "gender", "birthDate"}`(모두 필수) → `UserView`. 중복 409, 형식 오류 422.
- `PATCH /v1/account/preferences` `{"marketingOptIn": bool}` → `UserView`.
- `GET /v1/account/sharing` → `{"shares": [{"viewerUserId", "viewerName", "relation", "report": bool, "timeline": bool}]}`
  (같은 가족의 연결된 구성원마다 한 줄, 기본값 true). `PUT /v1/account/sharing` 같은 모양의 `shares` → 같은 응답.
- `GET /v1/families/{familyId}/members` 각 구성원에 추가: `acceptedAt: datetime|null`, `consentGranted: bool`,
  `nickname: str|null`, `birthDate: str|null`, `canRemove: bool`. `canInvite` = 가족 구성원 누구나 true.
  만든 사람도 다른 구성원과 같은 형식(`relation` 은 초대 때 적은 값, 만든 사람은 `"SELF"` 아님 — 만든 사람의 relation 은 비워 두고 `isCreator: true`).
- `DELETE /v1/families/{familyId}/members/{memberId}` → 204. 권한은 S4 규칙.
- `POST /v1/families/{familyId}/invitations`·`POST /v1/invitations/accept`·`POST /v1/calls` 는 모양 그대로, 역할 검사만 없앤다.
  가족이 아직 없는 사람의 첫 초대를 위해 `POST /v1/families` → `{"familyId"}`(이미 있으면 그 가족) 추가.
- `GET/PUT /v1/parents/{userId}/profile` 은 누구나 자기 것·같은 가족 것(질환 `conditions` + `concerns`).

### 구현에서 정한 세부(2026-10-30, S1·S2·S4 반영)
위 약속이 비워 둔 곳을 이렇게 채웠다. 안드로이드는 이 값에 맞춘다.

- `POST /v1/families`: 새로 만들면 201, 이미 있으면 200. 둘 다 `{"familyId"}`.
- 로그인: Google 로그인에 `role`을 안 보내면 가족을 만들지 않는다(`familyId: null`, 저장 역할은 `CHILD`).
  Google 로그인에 `role`을 보내거나 OTP·Apple 로그인(역할 기본값 `CHILD`)이면 예전처럼 자녀 계정에 가족이 바로 생긴다.
- 구성원 목록: 만든 사람의 `relation`은 빈 문자열 `""`(iOS·안드로이드 모델이 non-null 문자열이라 null 대신),
  `acceptedAt`은 가족을 만든 시각, `canRemove: false`. 대기 중 초대는 `acceptedAt: null`. 초대 코드(`invitation`)는
  구성원 모두에게 보인다. 순서는 예전처럼 초대받은 구성원들 다음에 만든 사람.
- `POST /v1/invitations/accept` 의 `status` 는 수락한 사람의 동의 상태(`CONSENT_GRANTED` 등)다.
- 닉네임: `^[가-힣A-Za-z0-9]{2,10}$`(한글은 완성형 음절만, 공백 불가), 대소문자 구분. 자기 닉네임을 다시 확인하면
  `available: true`. `birthDate`는 1900-01-01 ~ 오늘(KST).
- `PUT /v1/account/sharing`: 보낸 줄만 바뀐다. 같은 가족의 연결된 구성원이 아닌 `viewerUserId`가 있으면 422.
- `DELETE .../members/{memberId}`: 다른 가족·없는 id 는 404, 권한 없음 403. 나간 사람과 남은 구성원 사이의
  공유 행은 지워진다(다시 들어오면 모두 공개부터).
- 만든 사람(`memberId` = 만든 사람의 userId): 다른 사람이 내보내려 하면 409. **본인이 지우면 나가기**(2026-11-03) —
  가장 먼저 초대받아 들어온 연결된 구성원이 새 주인(`isCreator`)이 되고, 그 사람의 구성원 줄·초대는 지운다.
  넘길 사람이 없으면 대기 중 초대와 함께 가족을 지운다. 구성원 목록에서 만든 사람 본인 줄의 `canRemove` 는 true.
- 계정 삭제도 같다: 만든 가족에 연결된 구성원이 있으면 가족은 남고 주인만 넘어간다. 남은 사람이 없는 가족만 지운다.

## 안드로이드 (Collog-Android `main`)
| 단계 | 내용 | 필요한 서버 |
|---|---|---|
| A3 설정 | 메인 4줄, 계정 정보, 가족 구성원 관리, 가족 공유, 질환 프로필 요약 | S1·S2·S4 |
| A4 가입 | 온보딩 3장 → 로그인 → 약관 → (PASS, 꺼 둠) → 닉네임·성별·생년월일·초대 코드 → 본인 질환 → 홈. 역할 선택 없음 | S1·S2 |
| A5 카카오 로그인 | 온보딩 「카카오로 계속하기」 | S5 |
| A6 통화 분석·리포트 「나」 | 통화 직후 분석 화면, 리포트·타임라인에 본인 포함 | S3 |

## 병합
`feat/android-port`(PR #6) 다음에 이 브랜치를 검토받는다. `main` 병합은 서버 담당이 한다(main push = 운영 배포).
마이그레이션은 `0013` 부터.
