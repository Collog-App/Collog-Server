# 가족 대칭 구조(B안) 이행 계획

마지막 갱신: 2026-10-30 · 브랜치 `feat/family-symmetric`(`feat/android-port` 위)

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

### S3 두 사람 분석 (큼 — 팀·비용 확인 뒤)
- `CallParticipant(callId, userId, role, analysisStatus, speechSec)`, 참여자별 STT·추출·음향, 대상별 리포트.
- 오늘의 질문 대상 = 받는 사람의 프로필. 원본 폐기는 두 사람 분석이 모두 끝난 뒤.
- 분석 비용이 대략 두 배가 된다 → `operating-cost.html` 기준으로 다시 계산하고 팀 결정 뒤 착수.

### S4 구성원 관리·공유 범위 (중간)
- `DELETE /v1/families/{familyId}/members/{memberId}`: 대기 중 초대는 구성원 누구나, 연결된 구성원은
  가족을 만든 사람 또는 본인(나가기).
- 공유 범위: 각자 자기 기록을 어느 구성원에게 보일지 정한다. `family_shares(owner, viewer, report, timeline)`,
  기본값 모두 허용. `GET/PUT /v1/account/sharing`. S1 접근 권한이 이것을 본다.
- 마케팅 수신 동의: `users.marketing_opt_in` + `PATCH /v1/account/preferences`.

### S5 카카오 로그인 (카카오 디벨로퍼스 앱 키가 생긴 뒤)
- `POST /v1/auth/kakao` — 안드로이드 SDK 가 받은 토큰을 서버가 카카오에 확인. Google 로그인과 같은 계정 연결 방식.

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
